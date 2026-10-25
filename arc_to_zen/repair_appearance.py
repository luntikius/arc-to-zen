"""Appearance-only repair: validate a disposable copy, install patched sessions.

All non-session profile files are copied byte-for-byte. NSS is never exported.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
from collections import Counter
from pathlib import Path

from .appearance import apply_patches, capabilities, patches, source_appearance, appearance_digest
from .common import validate_staging_tree, MigrationError, database, private_tree, read_json
from .transaction import Transaction, marker, operation_lock, tree_digest, validate_target
from .zen import Gecko, check_sidebar, comparable_sidebar, read_lz4, write_lz4


def owner(args):
    current = marker(args.zen_root)
    if not current:
        raise MigrationError('Appearance repair requires a migrated Zen profile.')
    seen, run = set(), current
    while run not in seen:
        seen.add(run)
        d = Transaction(args.runtime, run).load()
        if Path(d['target']).resolve() != args.zen_root.resolve() or d['phase'] != 'installed':
            raise MigrationError('Appearance run does not belong to the installed Zen.')
        if run == args.run:
            return Transaction(args.runtime, current).load()
        run = d.get('appearance_of')
        if not run:
            break
    raise MigrationError('Selected run does not own the current Zen appearance.')


def detach(path):
    validate_staging_tree(path)
    for name in ('parent.lock', '.parentlock', 'lock'):
        (path / name).unlink(missing_ok=True)
    prefs = path / 'prefs.js'
    if prefs.exists():
        prefs.write_text('\n'.join(l for l in prefs.read_text().splitlines()
            if '"toolkit.profiles.' not in l and '"browser.profiles.' not in l) + '\n')
    # A source user.js could reintroduce StoreID. It is retained in the installed
    # copy; the disposable checker gets only the controlled Gecko preferences.
    (path / 'user.js').unlink(missing_ok=True)


def check_native_restore(expected, actual):
    # Gecko may refresh a navigation entry's display title while restoring a
    # page. Validate identities, URL histories, indices, ordering and appearance;
    # install the original documents, so even those original titles are retained.
    snapshots = [copy.deepcopy(expected), copy.deepcopy(actual)]
    for sidebar in snapshots:
        for tab in sidebar['tabs']:
            for entry in tab.get('entries', []):
                entry.pop('title', None)
    check_sidebar(*snapshots)


def repair_appearance(args, check_closed):
    old = owner(args)
    reset = getattr(args, 'reset_theme', None)
    if reset:
        matches = sum(s['uuid'] == reset for folder in old['profiles']
            for s in read_lz4(args.zen_root / 'Profiles' / folder / 'zen-sessions.jsonlz4')['spaces'])
        if matches != 1:
            raise MigrationError('Theme reset requires one existing Space UUID.')
    source = None if reset else source_appearance(args.arc_root)
    if args.dry_run:
        if reset:
            print('Native Zen theme reset: 1 Space. Read-only; no Arc appearance will be imported.')
            return marker(args.zen_root)
        counts = Counter()
        for folder in old['profiles']:
            sidebar = read_lz4(args.zen_root / 'Profiles' / folder / 'zen-sessions.jsonlz4')
            for kind, key in (('spaces', 'uuid'), ('folders', 'id')):
                counts[kind] += sum(bool(source[kind].get(o[key])) for o in sidebar[kind])
        print(f'Appearance candidates in saved sessions: {dict(counts)}. Read-only; native compatibility is checked during apply.')
        return marker(args.zen_root)
    check_closed()
    validate_target(args.zen_root)
    with operation_lock(args.runtime):
        old = owner(args)
        before = tree_digest(args.zen_root)
        source_before = None if reset else hashlib.sha256((args.arc_root / 'StorableSidebar.json').read_bytes()).hexdigest()
        source = None if reset else source_appearance(args.arc_root)
        tx = None
        try:
            skipped, totals = Counter(), Counter()
            results = {}
            with tempfile.TemporaryDirectory(prefix='appearance-', dir=args.runtime) as temp:
                for folder, meta in old['profiles'].items():
                    original = args.zen_root / 'Profiles' / folder
                    if not original.resolve().is_relative_to(args.zen_root.resolve()):
                        raise MigrationError('Invalid profile path.')
                    check = Path(temp) / folder
                    shutil.copytree(original, check, symlinks=True)
                    private_tree(check); detach(check)
                    sidebar = read_lz4(original / 'zen-sessions.jsonlz4')
                    state = read_lz4(original / 'sessionstore.jsonlz4')
                    with Gecko(args.zen_app, check) as g:
                        caps = capabilities(g)
                        if reset:
                            native = g.call('defaultTheme', None)
                            changes = {'spaces': [{'uuid': reset, 'theme': native} for s in sidebar['spaces']
                                if s['uuid'] == reset and s.get('theme') != native], 'folders': []}
                        else:
                            changes = patches(sidebar, source, caps, skipped)
                        if not any(changes.values()):
                            continue
                        expected = copy.deepcopy(sidebar); apply_patches(expected, changes)
                        live = g.call('state', None)
                        for w in live['windows']:
                            check_native_restore(sidebar, w)
                        g.call('applyAppearance', changes)
                        g.call('verifyAppearance', changes)
                    for _ in range(2):
                        with Gecko(args.zen_app, check) as g:
                            live = g.call('state', None)
                            for w in live['windows']:
                                check_native_restore(expected, w)
                            g.call('verifyAppearance', changes)
                    # Only patch original session documents. Browser startup may
                    # maintain its own databases; those test-copy changes never
                    # enter the installed profile.
                    for w in state['windows']:
                        apply_patches(w, changes)
                    results[folder] = (expected, state)
                    for kind in changes:
                        totals[kind] += len(changes[kind])
                if tree_digest(args.zen_root) != before:
                    raise MigrationError('Current Zen changed during appearance verification.')
                if not reset and hashlib.sha256((args.arc_root / 'StorableSidebar.json').read_bytes()).hexdigest() != source_before:
                    raise MigrationError('Arc appearance changed during preparation.')
                if not results:
                    print(f'Appearance already matches or is unsupported; no changes made. Skipped: {dict(skipped)}.')
                    return marker(args.zen_root)
                tx = Transaction.create(args.runtime, args.zen_root)
                d = tx.load(); d['appearance_of'] = old['run']
                d['registration_hashes'] = {name: hashlib.sha256((args.zen_root / name).read_bytes()).hexdigest()
                    for name in ('profiles.ini', 'installs.ini') if (args.zen_root / name).exists()}
                tx.save(d)
                shutil.copytree(args.zen_root, tx.stage, symlinks=True)
                metadata = copy.deepcopy(old['profiles'])
                for folder, m in metadata.items():
                    profile = tx.stage / 'Profiles' / folder
                    if folder in results:
                        sidebar, state = results[folder]
                        write_lz4(profile / 'zen-sessions.jsonlz4', sidebar)
                        write_lz4(profile / 'sessionstore.jsonlz4', state)
                    else:
                        sidebar = read_lz4(profile / 'zen-sessions.jsonlz4')
                    m['sidebar_hash'] = hashlib.sha256(json.dumps(comparable_sidebar(sidebar), sort_keys=True).encode()).hexdigest()
                    m['appearance_hash'] = appearance_digest(sidebar)
                    with database(profile / 'places.sqlite') as db:
                        history = db.execute('SELECT count(*) FROM moz_historyvisits').fetchone()[0]
                    logins = read_json(profile / 'logins.json') if (profile / 'logins.json').exists() else {}
                    m['counts'] = {'tabs': sum(not t.get('zenIsEmpty') for t in sidebar['tabs']),
                                   'spaces': len(sidebar['spaces']), 'history': history, 'logins': len(logins.get('logins', []))}
                tx.install(metadata, check_closed, expected_before=before, preserve_registration=True)
                print(f'Appearance updated: {dict(totals)}; skipped={dict(skipped)}. Run: {tx.directory.name}. Full Zen backup retained.')
                return tx.directory.name
        except BaseException:
            if tx:
                d = tx.load()
                if d['phase'] == 'preparing':
                    d['phase'] = 'failed'; tx.save(d)
            raise
