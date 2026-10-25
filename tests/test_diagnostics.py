import contextlib
import io
import json
import os
import stat
import tempfile
import threading
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from arc_to_zen import diagnostics as d
from arc_to_zen.cli import _main
from arc_to_zen.common import MigrationError, CHROME_EPOCH_US
from arc_to_zen.source import cookie_record
from arc_to_zen.appearance import theme
from test_appearance import CAPS, arc_theme


class DiagnosticsTests(unittest.TestCase):
    def test_disabled_has_no_files_and_enabled_is_private_and_thread_visible(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / 'runtime'
            with d.recording(root, False): d.emit('phase', operation='scan', status='ok')
            self.assertFalse(root.exists())
            with d.recording(root, True) as recorder:
                worker = threading.Thread(target=lambda: d.emit('phase', operation='scan', status='ok'))
                worker.start(); worker.join()
            rows = [json.loads(line) for line in recorder.path.read_text().splitlines()]
            self.assertEqual(rows[-1]['operation'], 'scan')
            self.assertEqual(stat.S_IMODE(recorder.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(recorder.path.parent.stat().st_mode), 0o700)

    def test_symlink_and_collision_are_not_followed_or_overwritten(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            target = root / 'target'; target.mkdir()
            runtime = root / 'runtime'; runtime.mkdir()
            (runtime / 'diagnostics').symlink_to(target)
            with self.assertRaises(MigrationError): d.Recorder(runtime)
            self.assertFalse(list(target.iterdir()))
            (runtime / 'diagnostics').unlink()
            with patch('arc_to_zen.diagnostics.uuid.uuid4') as uid, patch('arc_to_zen.diagnostics.time.strftime', return_value='fixed'):
                uid.return_value.hex = 'fixed'
                first = d.Recorder(runtime); first.close()
                before = first.path.read_bytes()
                with self.assertRaises(MigrationError): d.Recorder(runtime)
                self.assertEqual(first.path.read_bytes(), before)

    def test_allowlist_excludes_secrets_unknown_fields_and_exception_messages(self):
        secret = 'DO_NOT_LOG_secret.example_cookie_password_profile'
        with tempfile.TemporaryDirectory() as t:
            with d.recording(Path(t), True) as recorder:
                d.emit('cookie_skip', cookie=1, stage='source', reason='expired', value=secret, host=secret,
                       profile=secret, run=secret, categories=['cookies', secret])
                d.emit('theme', input_theme={'background': {'single': {'_0': {'style': {secret: secret},
                    'contentOverBackgroundAppearance': secret}}}, secret: secret}, output_theme={'opacity': .5})
                try: raise ValueError(secret)
                except ValueError as exc: d.error('source', exc)
            output = recorder.path.read_text()
            self.assertNotIn(secret, output)
            self.assertIn('ValueError', output)
            self.assertIn('unknown_fields', output)

    def test_recording_failure_is_visible_and_does_not_interrupt_work(self):
        with tempfile.TemporaryDirectory() as t:
            with d.recording(Path(t), True) as recorder:
                with patch.object(recorder.file, 'write', side_effect=OSError('private-secret')):
                    d.emit('phase', operation='transfer', status='started')
                self.assertTrue(recorder.failed)
                self.assertIn('incomplete', d.receipt()[0])
                d.emit('phase', operation='transfer', status='ok')
            self.assertNotIn('private-secret', recorder.path.read_text())

    def test_cli_records_before_discovery_and_reports_errors_and_cancellation(self):
        for failure, code in [(MigrationError('synthetic'), 1), (KeyboardInterrupt(), 130)]:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as t:
                with patch('arc_to_zen.cli.resolve_apps', side_effect=failure), contextlib.redirect_stderr(io.StringIO()) as stderr:
                    self.assertEqual(_main(['--runtime', t, '--diagnostics', 'inspect']), code)
                log = next((Path(t) / 'diagnostics').iterdir())
                self.assertIn(str(log), stderr.getvalue())
                rows = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertEqual(rows[0]['event'], 'session_start')
                self.assertEqual(rows[-1]['exit_code'], code)

    def test_creation_failure_stops_before_discovery(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / 'diagnostics').write_text('keep')
            with patch('arc_to_zen.cli.resolve_apps') as discover, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(_main(['--runtime', t, '--diagnostics', 'inspect']), 1)
            discover.assert_not_called()
            self.assertEqual((Path(t) / 'diagnostics').read_text(), 'keep')

    def test_dry_run_never_requests_secrets_and_keeps_diagnostics_outside_data(self):
        from fixtures import synthetic_arc
        from arc_to_zen.selection import Selection, catalog
        from arc_to_zen.transfer import scan
        from arc_to_zen.transaction import tree_digest
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as t:
            base = Path(t); arc = synthetic_arc(base / 'Arc')
            selection = Selection.from_args(SimpleNamespace(profiles=None, spaces=None, categories=['tabs', 'themes', 'cookies']), catalog(arc))
            before = tree_digest(arc)
            with d.recording(base / 'runtime', True) as recorder, patch('arc_to_zen.transfer.ArcDecryptor', side_effect=AssertionError('No Keychain')):
                scan(SimpleNamespace(arc_root=arc), selection)
            self.assertEqual(tree_digest(arc), before)
            self.assertIn('scan_summary', recorder.path.read_text())
            self.assertNotIn('cookie_read', recorder.path.read_text())

    def test_skip_reasons_and_run_summary_balance(self):
        with tempfile.TemporaryDirectory() as t:
            with d.recording(Path(t), True) as recorder:
                d.bind_run('20261025T100007Z-37a67cd2')
                ids = [d.cookie_read('private-profile') for _ in range(4)]
                d.cookie_skip(ids[0], 'source', 'expired')
                d.cookie_skip(ids[2], 'native', 'invalid_domain')
                d.cookie_summary(2)
            rows = [json.loads(line) for line in recorder.path.read_text().splitlines()]
            summary = rows[-1]
            self.assertEqual((summary['source'], summary['accepted'], summary['skipped']), (4, 2, 2))
            self.assertEqual(summary['reasons'], {'expired': 1, 'invalid_domain': 1})
            self.assertEqual({r['cookie'] for r in rows if r['event'] == 'cookie_skip'}, {ids[0], ids[2]})
            self.assertNotIn('private-profile', recorder.path.read_text())

    def test_source_reasons_without_secrets_or_keychain(self):
        row = dict(host_key='synthetic.example', name='private', value='value', encrypted_value=b'',
                   path='/', expires_utc=CHROME_EPOCH_US + 100000000, is_persistent=1,
                   is_secure=1, is_httponly=1, samesite=0)
        cases = [('expired', {}), ('partition_ancestor', {'top_frame_site_key':'https://top.example', 'has_cross_site_ancestor':1}),
                 ('source_type', {'source_type':99}), ('source_scheme', {'source_scheme':99}),
                 ('partition_format', {'top_frame_site_key':'file:///private', 'is_persistent':0}),
                 ('partition_port', {'top_frame_site_key':'https://top.example:444', 'is_persistent':0}),
                 ('cipher_unsupported', {'encrypted_value':b'v20synthetic', 'is_persistent':0}),
                 ('same_site', {'samesite':99, 'is_persistent':0})]
        for reason, changes in cases:
            reasons = []
            cookie_record({**row, **changes}, 23, lambda value: None, now_s=200, on_skip=reasons.append)
            self.assertEqual(reasons, [reason])

    def test_theme_diagnostics_distinguish_absent_unsupported_and_converted(self):
        with tempfile.TemporaryDirectory() as t:
            with d.recording(Path(t), True) as recorder:
                for index, value in enumerate([None, {'background': {'future': {}}}, arc_theme()], 1):
                    theme(value, CAPS, Counter(), index)
            rows = [json.loads(line) for line in recorder.path.read_text().splitlines()]
            themes = [r for r in rows if r['event'] == 'theme']
            self.assertEqual({r['status'] for r in themes}, {'absent', 'started', 'unsupported', 'converted'})
            self.assertTrue(any(r.get('output_theme', {}).get('gradientColors') for r in themes))


if __name__ == '__main__': unittest.main()
