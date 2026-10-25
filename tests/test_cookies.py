import os
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fixtures import synthetic_arc
from arc_to_zen.common import CHROME_EPOCH_US, MigrationError, database
from arc_to_zen.selection import Selection, catalog
from arc_to_zen.transaction import Transaction, tree_digest
from arc_to_zen.transfer import scan, transfer
from arc_to_zen.zen import Gecko, read_lz4
from arc_to_zen import diagnostics


@unittest.skipUnless(os.environ.get('ARC_TO_ZEN_INTEGRATION') == '1', 'Opt in to isolated Zen checks')
class CookieIntegrationTests(unittest.TestCase):
    def test_5016_records_balance_across_batches_and_survive_restarts(self):
        with tempfile.TemporaryDirectory(prefix='atz-large-cookie-test-') as t:
            base = Path(t); arc = synthetic_arc(base / 'Arc'); zen = base / 'zen'; zen.mkdir()
            (zen / 'profiles.ini').write_text('[General]\nVersion=2\n')
            for key in ('Default', 'Profile 1'):
                with sqlite3.connect(arc / 'User Data' / key / 'Cookies') as db:
                    rows = []
                    for i in range(2508):
                        valid = i < 1000
                        # Spread records across registrable domains to avoid a
                        # per-domain quota; 2000 accepted records fit native defaults.
                        rows.append((('.' if i % 2 else '') + f'c{i}.example', '/', 'cookie', 'synthetic-secret', b'',
                                     0 if valid else CHROME_EPOCH_US + 1000000, 1, 1, 0 if valid else 1, 0, 2, '', 0, 1))
                    db.executemany('INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
            args = SimpleNamespace(arc_root=arc, zen_root=zen, runtime=base / 'runs', zen_app=Path('/Applications/Zen.app'))
            selection = Selection.from_args(SimpleNamespace(profiles=None, spaces=None, categories=['tabs', 'cookies']), catalog(arc))
            with diagnostics.recording(args.runtime, True) as log:
                scanned = scan(args, selection, probe=True)
                self.assertEqual(scanned.counts['cookies'], 5016)
                with patch('arc_to_zen.transfer.ArcDecryptor') as decryptor:
                    result = transfer(args, scanned, lambda: None)
                    decryptor.return_value.assert_not_called()
            self.assertEqual((result['counts']['cookies'], result['skipped']['cookies']), (2000, 3016))
            rows = [json.loads(line) for line in log.path.read_text().splitlines()]
            summary = next(r for r in rows if r['event'] == 'cookie_summary')
            self.assertEqual((summary['source'], summary['accepted'], summary['skipped']), (5016, 2000, 3016))
            self.assertEqual(summary['reasons'], {'expired': 3016})
            self.assertEqual(len({r['cookie'] for r in rows if r['event'] == 'cookie_skip'}), 3016)
            self.assertEqual(sum(r['accepted'] for r in rows if r['event'] == 'cookie_batch'), 2000)
            final = next(r for r in rows if r['event'] == 'transfer_summary')
            self.assertEqual(final['counts']['cookies'], 2000)
            self.assertEqual(final['skipped_counts']['cookies'], 3016)
            for secret in ('synthetic-secret', 'c999.example', 'Your Arc'):
                self.assertNotIn(secret, log.path.read_text())
            self.assertEqual(len([r for r in rows if r['event'] == 'verification' and r['status'] == 'verified']), 2)
            Transaction(args.runtime, result['run']).rollback(False, lambda: None)
            self.assertTrue(log.path.exists())

    def test_evicted_cookie_stops_verification(self):
        cookie = dict(host='eviction.example', path='/', name='synthetic', value='secret', secure=True,
                      httpOnly=True, session=True, expiry=0, originAttributes={}, sameSite=2, schemeMap=2)
        with tempfile.TemporaryDirectory(prefix='atz-cookie-eviction-') as t:
            with Gecko(Path('/Applications/Zen.app'), Path(t) / 'profile') as g:
                # Force a native quota collision, not an explicit deletion.
                g.client.execute_script('Services.prefs.setIntPref("network.cookie.maxPerHost", 2); Services.prefs.setIntPref("network.cookie.quotaPerHost", 1);')
                cookies = [dict(cookie, name=f'cookie{i}') for i in range(1000)]
                g.call('cookies', cookies)
                with self.assertRaisesRegex(MigrationError, 'cookie missing'):
                    g.call('verifyData', {'logins': [], 'cookies': cookies})

    def test_rejected_cookies_are_skipped_without_weakening_or_restoring_them(self):
        with tempfile.TemporaryDirectory(prefix='atz-cookie-test-') as t:
            base = Path(t)
            arc = synthetic_arc(base / 'Arc')
            zen = base / 'zen'
            zen.mkdir()
            (zen / 'profiles.ini').write_text('[General]\nVersion=2\n')
            # The real browser uses wall time, so persistent fixtures need a
            # future expiry even when this test is run years after publication.
            expiry = CHROME_EPOCH_US + (int(time.time()) + 3600) * 1000000
            for index, key in enumerate(('Default', 'Profile 1')):
                records = [
                    ('cookie.example', 'persistent', 'synthetic-' + str(index), '/', 1, expiry, 1, ''),
                    ('cookie.example', 'session', 'session-' + str(index), '/', 1, 0, 0, ''),
                    ('cookie.example', 'chips', 'partition-' + str(index), '/', 1, 0, 0, 'https://top.example'),
                    ('.localhost', 'bad-local', 'private-synthetic-value', '/', 1, 0, 0, ''),
                    ('.127.0.0.1', 'bad-ip', 'private-synthetic-value', '/', 1, 0, 0, ''),
                    ('cookie.example', 'oversize', 'x' * 5000, '/', 1, 0, 0, ''),
                    ('cookie.example', 'insecure-none', 'private-synthetic-value', '/', 0, 0, 0, ''),
                    ('.cookie.example', '__Host-bad', 'private-synthetic-value', '/', 1, 0, 0, ''),
                ]
                with sqlite3.connect(arc / 'User Data' / key / 'Cookies') as db:
                    for host, name, value, path, secure, expires, persistent, partition in records:
                        db.execute('INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                   (host, path, name, value, b'', expires, secure, 1, persistent, 0, 2, partition, 0, 1))
            args = SimpleNamespace(arc_root=arc, zen_root=zen, runtime=base / 'runs', zen_app=Path('/Applications/Zen.app'))
            selection = Selection.from_args(SimpleNamespace(profiles=None, spaces=None, categories=['tabs', 'cookies']), catalog(arc))
            arc_before, zen_before = tree_digest(arc), tree_digest(zen)
            scanned = scan(args, selection, probe=True)
            self.assertEqual(scanned.counts['cookies'], 16)
            with patch('arc_to_zen.transfer.ArcDecryptor') as decryptor:
                decryptor.return_value.side_effect = AssertionError('No Keychain in synthetic cookie test')
                result = transfer(args, scanned, lambda: None)
                decryptor.return_value.assert_not_called()
            self.assertEqual(result['counts']['cookies'], 6)
            self.assertEqual(result['skipped']['cookies'], 10)
            tx = Transaction(args.runtime, result['run'])
            tx.verify()
            self.assertEqual(tree_digest(arc), arc_before)
            profile = next((zen / 'Profiles').iterdir())
            state = read_lz4(profile / 'sessionstore.jsonlz4')
            self.assertEqual({c['name'] for c in state['cookies']}, {'session', 'chips'})
            self.assertEqual(len(state['cookies']), 4)
            self.assertTrue(all(c['secure'] and c['httponly'] for c in state['cookies']))
            self.assertEqual(len({c['originAttributes']['userContextId'] for c in state['cookies']}), 2)
            self.assertTrue(all(c['originAttributes']['partitionKey'] == '(https,top.example)'
                                for c in state['cookies'] if c['name'] == 'chips'))
            # stage_profile already verified two native restarts. Read the
            # persisted stores too: rejected records must not reappear there.
            with database(profile / 'cookies.sqlite') as db:
                stored = db.execute('SELECT name,value,isSecure,isHttpOnly,originAttributes FROM moz_cookies').fetchall()
            self.assertEqual({r[0] for r in stored}, {'persistent'})
            self.assertEqual(len(stored), 2)
            self.assertTrue(all(r[2:4] == (1, 1) for r in stored))
            self.assertEqual(len({r[4] for r in stored if r[0] == 'persistent'}), 2)
            tx.rollback(False, lambda: None)
            self.assertEqual(tree_digest(zen), zen_before)

    def test_all_rejected_batch_and_unexpected_api_failure(self):
        cookie = dict(host='cookie.example', path='/', name='synthetic', value='test-secret',
                      secure=True, httpOnly=True, session=True, expiry=0,
                      originAttributes={}, sameSite=2, schemeMap=2)
        with tempfile.TemporaryDirectory(prefix='atz-cookie-api-test-') as t:
            with Gecko(Path('/Applications/Zen.app'), Path(t) / 'profile') as g:
                result = g.call('cookies', [dict(cookie, host='.localhost'), dict(cookie, host='.127.0.0.1')])
                self.assertEqual(result, {'inserted': 0, 'skipped': [0, 1],
                                         'reasons': {'0': 'invalid_domain', '1': 'invalid_domain'}})
                # Invalid API arguments share the invalid-host exception code.
                # They must fail, not silently skip an entire browser's data.
                with self.assertRaisesRegex(MigrationError, 'cookie API failure') as failure:
                    g.call('cookies', [dict(cookie, originAttributes=0)])
                self.assertNotIn('test-secret', str(failure.exception))
                self.assertNotIn('cookie.example', str(failure.exception))

    def test_domain_cookies_keep_leading_dot_and_host_only_cookies_stay_distinct(self):
        cookie = dict(path='/', name='synthetic', value='test-secret', secure=True, httpOnly=True,
                      session=True, expiry=0, originAttributes={}, sameSite=2, schemeMap=2)
        cookies = [dict(cookie, host=h) for h in ('cookie.example', '.cookie.example', 'localhost', '127.0.0.1')]
        cookies += [dict(cookie, host='.cookie.example', originAttributes={'partitionKey': '(https,top.example)'})]
        with tempfile.TemporaryDirectory(prefix='atz-domain-cookies-') as t:
            with Gecko(Path('/Applications/Zen.app'), Path(t) / 'profile') as g:
                self.assertEqual(g.call('cookies', cookies), {'inserted': 5, 'skipped': [], 'reasons': {}})
                g.call('verifyData', {'logins': [], 'cookies': cookies})
                hosts = g.client.execute_script('return [...Services.cookies.cookies].map(c => c.host)')
                self.assertEqual(set(hosts),
                                 {'cookie.example', '.cookie.example', 'localhost', '127.0.0.1'})


class CookieReceiptTests(unittest.TestCase):
    def test_success_shows_transferred_and_skipped_cookies(self):
        from arc_to_zen.wizard import finished
        from unittest.mock import Mock
        ui = Mock()
        ui.choose.return_value = 'open'
        result = {'counts': {'spaces': 2, 'tabs': 10, 'logins': 0, 'cookies': 6},
                  'skipped': {'cookies': 10}, 'backup': '/synthetic/backup', 'run': 'synthetic'}
        with patch('arc_to_zen.wizard.subprocess.run') as launch:
            launch.return_value.returncode = 0
            finished(ui, result, SimpleNamespace(zen_app=Path('/Synthetic/Zen.app')), demo=False)
        self.assertIn('Cookies: 6 transferred · 10 skipped.', ui.choose.call_args.args[1])
        self.assertIn('Cookies: 6 transferred · 10 skipped.', ui.receipt)
