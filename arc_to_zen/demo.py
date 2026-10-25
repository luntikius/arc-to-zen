"""Deterministic in-memory demo. No discovery, filesystem or Keychain access."""
import time
from .common import MigrationError
from .transfer import Scan

CATALOG = {'profiles': [{'id': 'personal', 'name': 'Personal'}, {'id': 'work', 'name': 'Work'}],
    'spaces': [{'id': 'personal-space', 'profile': 'personal', 'name': 'Personal'},
               {'id': 'projects-space', 'profile': 'personal', 'name': 'Projects'},
               {'id': 'work-space', 'profile': 'work', 'name': 'Work'}]}


class Demo:
    def __init__(self, failure=None, delay=.1):
        self.failure, self.delay = failure, delay

    def inventory(self, emit):
        for stage in ('Reading profile names', 'Discovering Spaces'):
            for _ in range(8): emit(stage); time.sleep(self.delay)
        return CATALOG

    def scan(self, selection, emit):
        selection.validate(CATALOG)
        for stage in ('Reading selected Spaces', 'Counting visits and archived tabs',
                      'Counting encrypted records — no decryption', 'Checking compatibility'):
            for i in range(8): emit(stage); time.sleep(self.delay)
        if self.failure == 'scan': raise MigrationError('Source database is locked. No data changed.')
        n, spaces = len(selection.profiles), len(selection.spaces)
        c = selection.categories
        counts = {'profiles': n, 'spaces': spaces if 'tabs' in c else n,
            'tabs': spaces * 12 if 'tabs' in c else 0, 'folders': spaces * 3 if 'tabs' in c else 0,
            'history': (n * 1500 if 'history' in c else 0) + (n * 40 if 'archive' in c else 0), 'logins': n * 24 if 'passwords' in c else 0,
            'cookies': n * 130 if 'cookies' in c else 0, 'bookmarks': n * 18 if 'bookmarks' in c else 0,
            'forms': n * 11 if 'forms' in c else 0, 'addresses': n if 'addresses' in c else 0,
            'cards': n if 'cards' in c else 0, 'archive': n * 40 if 'archive' in c else 0,
            'themes': spaces if 'themes' in c else 0, 'icons': spaces * 3 if 'icons' in c else 0}
        return Scan(selection, 'synthetic', counts, {}, [])

    def transfer(self, scan, emit):
        for stage in ('Preparing selected data', 'Transferring tabs & folders', 'Transferring selected browser data',
                      'Verifying restored Zen', 'Backing up existing Zen', 'Installing verified profile'):
            for i in range(10): emit(stage, i, 10); time.sleep(self.delay)
            if self.failure == 'transfer' and stage == 'Verifying restored Zen':
                raise MigrationError('Verification failed. Existing Zen remains unchanged.')
        return {'run': '20261024-120000', 'backup': '~/Library/Application Support/ArcToZenMigration/20261024-120000/backup', 'counts': scan.counts, 'skipped': {}}
