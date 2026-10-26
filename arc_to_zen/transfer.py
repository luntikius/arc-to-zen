"""Selection-aware scan and transfer; progress contains stages and counts only."""
import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from .common import MigrationError, database, private_tree, table_exists
from .crypto import ArcDecryptor
from .selection import catalog
from .source import (discover, build_sidebars, identifier, read_history, add_archive,
                     read_bookmarks, read_passwords, read_cookies, read_web_data)
from .merge import merge_profiles
from .zen import Gecko, stage_profile, comparable_sidebar
from .transaction import Transaction, operation_lock, validate_target, tree_digest
from .platforms import preflight
from . import diagnostics


class Cancelled(MigrationError):
    pass


def quiet(stage, done=None, total=None):
    pass


def source_stamp(root, selection):
    files = {root / 'User Data/Local State', root / 'StorableSidebar.json'}
    cats = selection.categories
    if 'tabs' in cats:
        files.update(root / n for n in ('StorableWindows.json', 'StorableSessionRestorationData.json'))
    if 'archive' in cats:
        files.add(root / 'StorableArchiveItems.json')
    for key in selection.profiles:
        p = root / 'User Data' / key
        names = []
        for cat, name in (('history', 'History'), ('passwords', 'Login Data'), ('bookmarks', 'Bookmarks')):
            if cat in cats: names.append(name)
        if cats & {'forms', 'addresses', 'cards'}: names.append('Web Data')
        if 'cookies' in cats: names.append('Network/Cookies' if (p / 'Network/Cookies').exists() else 'Cookies')
        for name in names:
            files.update(p / (name + suffix) for suffix in ('', '-wal', '-shm', '-journal') if suffix != '-shm')
        if 'tabs' in cats: files.update((p / 'Sessions').glob('*'))
    digest = hashlib.sha256(json.dumps(selection.document(), sort_keys=True).encode())
    for path in sorted(files):
        digest.update(str(path.relative_to(root)).encode())
        if path.is_file():
            with path.open('rb') as f:
                for block in iter(lambda: f.read(1024 * 1024), b''): digest.update(block)
        else: digest.update(b'<missing>')
    return digest.hexdigest()


@diagnostics.traced('source')
def collect(root, selection, secrets=False, emit=quiet):
    data = catalog(root); selection.validate(data)
    all_profiles = discover(root)
    profiles = {k: p for k, p in all_profiles.items() if k in selection.profiles}
    cats = selection.categories
    emit('Reading selected Spaces')
    mapping = {s['id']: s['profile'] for s in data['spaces']}
    if 'tabs' in cats:
        build_sidebars(root, profiles, selection.spaces, set(all_profiles))
    else:
        for p in profiles.values():
            p.sidebar = {k: [] for k in ('tabs', 'folders', 'groups', 'splitViewData', 'spaces')}
    for p in profiles.values():
        for kind in ('spaces', 'folders'):
            for item in p.sidebar[kind]:
                appearance = item.get('_arcAppearance', {})
                item['_arcAppearance'] = {k: v for k, v in appearance.items()
                    if (k == 'windowTheme' and 'themes' in cats) or (k == 'iconType' and 'icons' in cats)}
    decrypt = ArcDecryptor() if secrets and cats & {'passwords', 'cookies', 'cards'} else None
    for index, p in enumerate(profiles.values(), 1):
        emit('Reading selected profile data', index, len(profiles))
        if 'history' in cats: read_history(p)
        if 'bookmarks' in cats: read_bookmarks(p)
        web = cats & {'forms', 'addresses', 'cards'}
        if not secrets: web -= {'cards'}
        if web: read_web_data(p, decrypt, web)
        if secrets and 'passwords' in cats: read_passwords(p, decrypt)
        if secrets and 'cookies' in cats: read_cookies(p, decrypt)
    if 'archive' in cats:
        emit('Matching archived visits')
        visits_before = sum(len(p.history) for p in profiles.values())
        skipped = add_archive(root, profiles, mapping, allow_history_fallback=len(profiles) == len(all_profiles))
        next(iter(profiles.values())).archived_visits = sum(len(p.history) for p in profiles.values()) - visits_before
        next(iter(profiles.values())).skipped['archive'] += skipped
    return merge_profiles(list(profiles.values()))


def counts(p):
    def bookmarks(nodes):
        return sum(1 if 'url' in n else bookmarks(n.get('children', [])) for n in nodes)
    result = {k: len(getattr(p, k)) for k in ('history', 'logins', 'cookies', 'forms', 'addresses', 'cards')}
    result.update(profiles=len(p.containers), spaces=p.imported_space_count if p.imported_space_count is not None else len(p.sidebar['spaces']),
        tabs=sum(not t.get('zenIsEmpty') for t in p.sidebar['tabs']), folders=len(p.sidebar['folders']),
        bookmarks=bookmarks(p.bookmarks), archive=p.archived_visits,
        themes=p.theme_count if p.theme_count is not None else sum(bool(s.get('theme') or s.get('_arcAppearance', {}).get('windowTheme')) for s in p.sidebar['spaces']),
        icons=sum(bool(s.get('icon') or s.get('userIcon') or s.get('_arcAppearance', {}).get('iconType'))
                  for s in p.sidebar['spaces'] + p.sidebar['folders']))
    return result


@dataclass
class Scan:
    selection: object
    stamp: str
    counts: dict
    skipped: dict
    warnings: list
    zen_stamp: str = None


@diagnostics.traced('scan')
def scan(args, selection, emit=quiet, probe=False):
    diagnostics.emit('selection', categories=selection.categories, profiles=len(selection.profiles), spaces=len(selection.spaces))
    emit('Checking selected source files')
    before = source_stamp(args.arc_root, selection)
    p = collect(args.arc_root, selection, emit=emit)
    result = counts(p)
    warnings = ['Profile-level history, passwords and cookies cover the whole selected profile.',
                'Encrypted records are counted now; compatibility is checked during transfer.']
    for key in selection.profiles:
        path = args.arc_root / 'User Data' / key
        for category, file, table, column in (
            ('passwords', 'Login Data', 'logins', 'logins'), ('cookies', 'Cookies', 'cookies', 'cookies'),
            ('cards', 'Web Data', 'credit_cards', 'cards')):
            if category not in selection.categories: continue
            if category == 'cookies' and (path / 'Network/Cookies').exists(): file = 'Network/Cookies'
            if category == 'cards' and not (path / file).exists(): continue
            with database(path / file) as db:
                if category == 'cards' and not table_exists(db, table): continue
                where = ' WHERE blacklisted_by_user=0' if category == 'passwords' else ''
                result[column] += db.execute('SELECT count(*) FROM ' + table + where).fetchone()[0]
    if probe:
        preflight(args)
        emit('Checking installed Zen capabilities')
        with tempfile.TemporaryDirectory(prefix='arc-to-zen-capabilities-') as temp:
            with Gecko(args.zen_app, Path(temp) / 'profile') as g:
                g.call('capabilities', None)
                if selection.categories & {'themes', 'icons'}:
                    from .appearance import capabilities, resolve_sidebar
                    resolve_sidebar(p.sidebar, capabilities(g), p.skipped)
                    mapped = counts(p)
                    result.update(themes=mapped['themes'], icons=mapped['icons'])
        if selection.categories & {'themes', 'icons'}:
            warnings.append('Themes and icons are approximated within native Zen capabilities.')
    if before != source_stamp(args.arc_root, selection):
        raise MigrationError('Arc changed during scanning. Close Arc and scan again.')
    diagnostics.emit('scan_summary', counts=result, skipped_counts=dict(p.skipped), status='ok')
    return Scan(selection, before, result, dict(p.skipped), warnings,
                tree_digest(args.zen_root) if probe else None)


@diagnostics.traced('transfer')
def transfer(args, scanned, check_closed, emit=quiet):
    check_closed(); validate_target(args.zen_root); preflight(args)
    if source_stamp(args.arc_root, scanned.selection) != scanned.stamp:
        raise MigrationError('Arc or the selection changed. Scan and confirm again.')
    if scanned.zen_stamp is not None and tree_digest(args.zen_root) != scanned.zen_stamp:
        raise MigrationError('Zen changed after confirmation. Scan and confirm again.')
    with operation_lock(args.runtime):
        tx = Transaction.create(args.runtime, args.zen_root)
        try:
            emit('Preparing selected data')
            p = collect(args.arc_root, scanned.selection, secrets=True, emit=emit)
            folder = identifier('profile', 'combined')
            meta = stage_profile(args.zen_app, tx.stage / 'Profiles' / folder, p, emit=emit)
            meta.pop('sidebar', None)
            meta['sidebar_hash'] = hashlib.sha256(json.dumps(comparable_sidebar(p.sidebar), sort_keys=True).encode()).hexdigest()
            private_tree(tx.stage)
            if source_stamp(args.arc_root, scanned.selection) != scanned.stamp:
                raise MigrationError('Arc changed during migration; no replacement performed.')
            tx.install({folder: meta}, check_closed, expected_before=scanned.zen_stamp, emit=emit)
            # The exchange is complete. Cancellation no longer interrupts verification.
            tx.verify()
            diagnostics.emit('transfer_summary', counts=counts(p), skipped_counts=dict(p.skipped), status='verified')
            return {'run': tx.directory.name, 'backup': str(tx.directory / 'backup'),
                    'counts': counts(p), 'skipped': dict(p.skipped)}
        except BaseException as exc:
            journal = tx.load()
            if journal['phase'] == 'preparing':
                journal['phase'] = 'failed'; tx.save(journal)
            if isinstance(exc, (MigrationError, KeyboardInterrupt)):
                raise MigrationError(f'{exc or "Interrupted"} Run: {tx.directory.name}. '
                    'Use rollback --run with this ID to reconcile the operation.') from None
            raise MigrationError(f'Migration stopped. Run: {tx.directory.name}. '
                'Use rollback --run with this ID to reconcile the operation.') from None
