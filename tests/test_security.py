"""Security boundaries exercised only with disposable synthetic data."""
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from arc_to_zen.common import MigrationError, atomic_json, private_opener, validate_staging_tree
from arc_to_zen.transaction import Transaction, operation_lock
from arc_to_zen.zen import Gecko

ROOT = Path(__file__).resolve().parents[1]


class SecurityTests(unittest.TestCase):
    def test_atomic_json_never_follows_predictable_temp_link(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); outside = root / 'outside'; outside.write_text('retained')
            (root / 'journal.json.tmp').symlink_to(outside)
            target = root / 'journal.json'
            atomic_json(target, {'phase': 'preparing'})
            self.assertEqual(outside.read_text(), 'retained')
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(TypeError): atomic_json(target, object())
            self.assertEqual(json.loads(target.read_text()), {'phase': 'preparing'})
            self.assertFalse(list(root.glob('.journal.json-*')))

    def test_lock_and_private_writer_reject_links(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); outside = root / 'outside'; outside.write_text('retained')
            link = root / '.lock'; link.symlink_to(outside)
            with self.assertRaises(OSError):
                with operation_lock(root): self.fail('lock followed symlink')
            with self.assertRaises(OSError):
                with open(link, 'w', opener=private_opener): pass
            self.assertEqual(outside.read_text(), 'retained')
            (root / 'run').symlink_to(root, target_is_directory=True)
            with self.assertRaises(MigrationError): Transaction(root, 'run')

    def test_staging_rejects_links_before_changing_preferences(self):
        from arc_to_zen.repair_appearance import detach
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); profile = root / 'profile'; profile.mkdir()
            outside = root / 'prefs.js'; outside.write_text('retained')
            (profile / 'prefs.js').symlink_to(outside)
            with self.assertRaises(MigrationError): detach(profile)
            self.assertEqual(outside.read_text(), 'retained')
            (profile / 'prefs.js').unlink()
            (profile / 'parent.lock').symlink_to('unavailable-lock')
            validate_staging_tree(profile)
            os.mkfifo(profile / 'pipe')
            with self.assertRaises(MigrationError): validate_staging_tree(profile)

    def test_staging_kills_and_reaps_after_failed_termination(self):
        g = Gecko(Path('/unused'), Path('/unused'))
        proc = Mock(); client = Mock(); g.process = proc; g.client = client
        proc.poll.return_value = None
        proc.wait.side_effect = [subprocess.TimeoutExpired('zen', 30), subprocess.TimeoutExpired('zen', 10), 0]
        with self.assertRaises(MigrationError): g.close()
        proc.terminate.assert_called_once(); proc.kill.assert_called_once()
        self.assertEqual(proc.wait.call_count, 3)
        client.cleanup.assert_called_once()
        self.assertIsNone(g.process)

    def test_staging_reaps_when_shutdown_is_interrupted(self):
        g = Gecko(Path('/unused'), Path('/unused'))
        proc = Mock(); client = Mock(); g.process = proc; g.client = client
        proc.poll.return_value = None; proc.wait.side_effect = [KeyboardInterrupt(), 0]
        with self.assertRaises(KeyboardInterrupt): g.close()
        proc.terminate.assert_called_once(); client.cleanup.assert_called_once()

    def test_cancelled_startup_always_closes_staging_process(self):
        with tempfile.TemporaryDirectory() as t:
            g = Gecko(Path('/unused'), Path(t))
            with patch('arc_to_zen.common.app_version', return_value='test'), \
                 patch('marionette_driver.marionette.Marionette') as client, \
                 patch('arc_to_zen.zen.subprocess.Popen'), patch.object(g, 'close') as close:
                client.return_value.start_session.side_effect = KeyboardInterrupt()
                with self.assertRaises(KeyboardInterrupt): g.__enter__()
                close.assert_called_once()

    def test_cancellation_at_each_transfer_boundary_preserves_browsers(self):
        from types import SimpleNamespace
        from fixtures import synthetic_arc
        from arc_to_zen.selection import Selection, catalog
        from arc_to_zen.transfer import Cancelled, scan, transfer
        from arc_to_zen.transaction import tree_digest
        stages = ('Preparing selected data', 'Reading selected Spaces', 'Reading selected profile data',
                  'Transferring history', 'Verifying restored Zen', 'Backing up existing Zen', 'Installing verified profile')
        for stop in stages:
            with self.subTest(stage=stop), tempfile.TemporaryDirectory() as t:
                root = Path(t); arc = synthetic_arc(root / 'Arc'); zen = root / 'zen'; zen.mkdir()
                (zen / 'profiles.ini').write_text('[General]\nVersion=2\n')
                args = SimpleNamespace(arc_root=arc, zen_root=zen, runtime=root / 'runs', zen_app=Path('/unused'))
                selection = Selection.from_args(SimpleNamespace(categories=['tabs']), catalog(arc))
                scanned = scan(args, selection)
                before = tree_digest(arc), tree_digest(zen)
                def emit(stage, *args):
                    if stage == stop: raise Cancelled('Cancelled')
                def stage_profile(app, path, profile, emit):
                    path.mkdir(parents=True)
                    emit('Transferring history'); emit('Verifying restored Zen')
                    return {'name': 'Synthetic'}
                with patch('arc_to_zen.transfer.preflight'), patch('arc_to_zen.transfer.stage_profile', stage_profile), \
                     patch('arc_to_zen.transaction.exchange', side_effect=AssertionError('No exchange')):
                    with self.assertRaises(MigrationError): transfer(args, scanned, lambda: None, emit)
                self.assertEqual((tree_digest(arc), tree_digest(zen)), before)
                journal = next(args.runtime.glob('*/journal.json'))
                self.assertEqual(json.loads(journal.read_text())['phase'], 'failed')

    def test_unknown_cookie_scheme_is_skipped_before_decryption(self):
        from arc_to_zen.source import cookie_record
        decrypt = Mock(side_effect=AssertionError("Must not decrypt unsupported cookies"))
        self.assertIsNone(cookie_record({"source_scheme": 99}, 24, decrypt))
        decrypt.assert_not_called()

    def test_cli_recovery_commands_take_operation_lock(self):
        from arc_to_zen.cli import main
        with tempfile.TemporaryDirectory() as t:
            for command in ('rollback', 'repair-registration'):
                with self.subTest(command=command), patch('arc_to_zen.cli.resolve_apps'), \
                     patch('arc_to_zen.cli.paths'), patch('arc_to_zen.cli.closed'), \
                     patch('arc_to_zen.cli.compatibility'), patch('arc_to_zen.cli.Transaction') as tx:
                    self.assertEqual(main(['--runtime', t, command, '--run', 'synthetic']), 0)
                    self.assertTrue((Path(t) / '.lock').is_file())
                    operation = tx.return_value.rollback if command == 'rollback' else tx.return_value.repair_registration
                    operation.assert_called_once()

    def test_release_extractor_rejects_unsafe_members(self):
        script = (ROOT / 'install.sh').read_text().split("<<'PYEXTRACT'\n", 1)[1].split('\nPYEXTRACT', 1)[0]
        cases = [('arc-to-zen-0.4.2rc1/ok.py', tarfile.REGTYPE, True),
                 ('arc-to-zen-0.4.2rc1/../../escape', tarfile.REGTYPE, False),
                 ('/absolute', tarfile.REGTYPE, False),
                 ('other-prefix/code.py', tarfile.REGTYPE, False),
                 ('arc-to-zen-0.4.2rc1/link', tarfile.SYMTYPE, False),
                 ('arc-to-zen-0.4.2rc1/hard', tarfile.LNKTYPE, False),
                 ('arc-to-zen-0.4.2rc1/pipe', tarfile.FIFOTYPE, False)]
        for name, kind, accepted in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as t:
                root = Path(t); archive = root / 'release.tar.gz'; out = root / 'out'; out.mkdir()
                with tarfile.open(archive, 'w:gz') as tar:
                    member = tarfile.TarInfo(name); member.type = kind
                    member.linkname = '../../escape'
                    member.size = 2 if kind == tarfile.REGTYPE else 0
                    tar.addfile(member, io.BytesIO(b'ok') if member.size else None)
                result = subprocess.run([sys.executable, '-c', script, str(archive), str(out), '0.4.2rc1'], capture_output=True)
                self.assertEqual(result.returncode == 0, accepted)
                self.assertFalse((root / 'escape').exists())

    def test_installer_waits_for_owned_child_before_cleanup(self):
        # Run just the real supervision code, no downloads or browser access.
        script = (ROOT / 'install.sh').read_text()
        supervision = script.split('CHILD=\n', 1)[1].split('/bin/mkdir "$SESSION/tmp"', 1)[0]
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); child = root / 'child.py'; receipt = root / 'receipt'
            child.write_text('import signal,time,sys\ndef stop(*args):\n time.sleep(.2)\n open(sys.argv[1],"w").write("stopped")\n sys.exit(0)\nsignal.signal(signal.SIGTERM,stop)\nprint("ready",flush=True)\nwhile True: time.sleep(.1)\n')
            shell = 'set -eu\nCHILD=\n' + supervision + '\ntrap \'test -f "$2" || exit 99\' EXIT\nrun_owned "$1" "$3" "$2"\n'
            p = subprocess.Popen(['/bin/sh', '-c', shell, 'installer', sys.executable, str(receipt), str(child)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(p.stdout.readline().strip(), 'ready')
                p.send_signal(signal.SIGTERM)
                p.communicate(timeout=10)
                self.assertEqual(p.returncode, 130)
                self.assertEqual(receipt.read_text(), 'stopped')
            finally:
                if p.poll() is None: p.kill(); p.wait()
