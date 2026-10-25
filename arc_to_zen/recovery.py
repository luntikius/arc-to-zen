"""Backup discovery and confirmation-bound access to the existing rollback."""
import hashlib
import time
from pathlib import Path

from .common import MigrationError
from .transaction import Transaction, marker, operation_lock, tree_digest


class Recovery:
    def __init__(self, runtime, target, check_closed):
        self.runtime, self.target, self.check_closed = runtime, target.resolve(), check_closed

    def backups(self):
        if self.runtime.is_symlink(): raise MigrationError('Backup directory cannot be a symlink.')
        entries, unavailable = [], 0
        for path in sorted(self.runtime.glob('*/journal.json'), reverse=True):
            try:
                tx = self.transaction(path.parent.name)
                data = tx.load()
                if data['phase'] in ('installed', 'applying', 'rolling_back') and (tx.directory/'backup').is_dir():
                    entries.append({'run': data['run'], 'backup': str(tx.directory/'backup'), 'phase': data['phase']})
            except (MigrationError, OSError, ValueError, KeyError, TypeError):
                unavailable += 1
        return entries, unavailable

    def transaction(self, run):
        tx = Transaction(self.runtime, run)
        if self.runtime.is_symlink() or tx.directory.is_symlink() or tx.journal.is_symlink():
            raise MigrationError('Backup paths cannot be symlinks.')
        data = tx.load()
        if Path(data['target']).resolve() != self.target or Path(data['target']).is_symlink():
            raise MigrationError('This backup belongs to a different Zen data directory.')
        return tx

    def preview(self, run, emit):
        self.check_closed()
        tx = self.transaction(run)
        data = tx.load()
        if data['phase'] not in ('installed', 'applying', 'rolling_back'):
            raise MigrationError('This backup is no longer available for rollback.')
        emit('Checking backup integrity')
        if (tx.directory/'backup').is_symlink() or tree_digest(tx.directory/'backup') != data['before']:
            raise MigrationError('Backup integrity check failed.')
        emit('Checking current Zen data')
        current = tree_digest(self.target)
        if marker(self.target) != run and not (data['phase'] in ('applying', 'rolling_back') and current == data['before']):
            raise MigrationError('Restore the latest migration first. This run does not own the current Zen data.')
        return {'run': run, 'backup': str(tx.directory/'backup'),
                'changed': current != data['installed'] and current != data['before'],
                'current': current, 'journal': hashlib.sha256(tx.journal.read_bytes()).hexdigest()}

    def restore(self, preview, emit):
        with operation_lock(self.runtime):
            self.check_closed()
            tx = self.transaction(preview['run'])
            if hashlib.sha256(tx.journal.read_bytes()).hexdigest() != preview['journal'] or tree_digest(self.target) != preview['current']:
                raise MigrationError('Zen or the backup journal changed. Review the backup again before restoring.')
            tx.rollback(preview['changed'], self.check_closed, expected_current=preview['current'], emit=emit)
            data = tx.load()
            if tree_digest(self.target) != data['before']:
                raise MigrationError('Restore verification failed. Keep the backup and run journal.')
            return {'run': preview['run'], 'backup': preview['backup'], 'retained': data.get('restored_copy')}


class DemoRecovery:
    """In-memory recovery screens; never enumerate or restore local backups."""
    def __init__(self, delay=.1): self.delay = delay

    def backups(self):
        return [{'run': '20261024-120000', 'backup': '~/Library/Application Support/ArcToZenMigration/20261024-120000/backup', 'phase': 'installed'}], 0

    def preview(self, run, emit):
        emit('Checking backup integrity'); time.sleep(self.delay)
        return dict(self.backups()[0][0], changed=True)

    def restore(self, preview, emit):
        for stage in ('Checking backup integrity', 'Preparing restored Zen', 'Restoring Zen'):
            emit(stage); time.sleep(self.delay)
        return dict(preview, retained='~/Library/Application Support/ArcToZenMigration/20261024-120000/restore')
