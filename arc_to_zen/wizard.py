"""Five-screen interactive wizard. Demo and real operations share the UI only."""
import subprocess
import time
from pathlib import Path
from .common import MigrationError, app_version
from .platforms import applications, resolve_apps, BUNDLE_IDS
from .selection import CATEGORIES, DEPENDENCIES, Selection, catalog
from .terminal import Terminal
from .transfer import scan, transfer
from . import diagnostics


def selection_screen(ui, data, selection):
    cursor = 0
    while True:
        profiles, spaces, cats = map(set, (selection.profiles, selection.spaces, selection.categories))
        rows = [('Select All', ('all', None))]
        for p in data['profiles']:
            rows.append((f"[{'x' if p['id'] in profiles else ' '}] Profile: {p['name']}", ('profile', p['id'])))
            for s in data['spaces']:
                if s['profile'] == p['id']:
                    rows.append((f"    [{'x' if s['id'] in spaces else ' '}] {s['name']}", ('space', s['id'])))
        for key, label in CATEGORIES.items():
            suffix = f' (requires {DEPENDENCIES[key]})' if key in DEPENDENCIES and DEPENDENCIES[key] not in cats else ''
            rows.append((f"[{'x' if key in cats else ' '}] {label}{suffix}", ('category', key)))
        rows += [('Continue to review', ('continue', None)), ('Back', ('back', None))]
        choice = ui.choose('Choose what comes with you', [
            'Profile-level data covers the whole selected profile, even if you select only one Space.',
            'A profile without selected Spaces gets an empty Space.',
            'Disabled categories are not read or decrypted.'], rows, cursor=cursor, bottom_actions=2)
        if isinstance(choice, str): return choice, selection
        cursor = next(i for i, (_, value) in enumerate(rows) if value == choice)
        kind, value = choice
        if kind == 'continue':
            try: selection.validate(data); return 'continue', selection
            except MigrationError as exc:
                ui.choose('Check your selection', [str(exc)], [('Back', 'back')]); continue
        if kind == 'back': return 'back', selection
        if kind == 'profile':
            owned = {s['id'] for s in data['spaces'] if s['profile'] == value}
            if value in profiles: profiles.remove(value); spaces -= owned
            else: profiles.add(value); spaces |= owned
        if kind == 'space':
            if value in spaces: spaces.remove(value)
            else:
                spaces.add(value); profiles.add(next(s['profile'] for s in data['spaces'] if s['id'] == value))
        if kind == 'category':
            if value in cats:
                cats.remove(value); cats -= {k for k, parent in DEPENDENCIES.items() if parent == value}
            else:
                cats.add(value)
                if value in DEPENDENCIES: cats.add(DEPENDENCIES[value])
        if kind == 'all':
            profiles = {p['id'] for p in data['profiles']}; spaces = {s['id'] for s in data['spaces']}; cats = set(CATEGORIES)
        selection = Selection(frozenset(profiles), frozenset(spaces), frozenset(cats))


def detect(args, ui):
    for kind in BUNDLE_IDS:
        given = getattr(args, kind + '_app')
        choices = [given] if given else applications(kind)
        if not choices:
            raise MigrationError(f'{kind.title()} not found. Install it or specify --{kind}-app /path/to/application.')
        if len(choices) > 1:
            selected = ui.choose('Choose ' + kind.title(), ['Several installations were found.'],
                [(str(p), p) for p in choices], stage=1)
            if isinstance(selected, str): return False
        else: selected = choices[0]
        setattr(args, kind + '_app', selected)
    resolve_apps(args)
    return True


def ensure_closed(args, ui, stage):
    from .cli import closed
    try: closed([args.arc_app, args.zen_app]); return True
    except MigrationError: pass
    choice = ui.choose('Close browsers before scanning', [
        'Arc and Zen must be fully closed to read consistent data.',
        'Continue asks macOS to quit both apps normally.'],
        [('Back', 'back'), ('Continue: close browsers & scan', 'close')], stage=stage)
    if choice != 'close': return False
    for bundle in BUNDLE_IDS.values():
        script = f'if application id "{bundle}" is running then tell application id "{bundle}" to quit'
        try:
            result = subprocess.run(['/usr/bin/osascript', '-e', script], capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            raise MigrationError('Browser close timed out. Close both browsers manually and retry.') from None
        if result.returncode:
            raise MigrationError('Could not close browsers. Close both browsers manually and retry.')
    for _ in range(30):
        try: closed([args.arc_app, args.zen_app]); return True
        except MigrationError: time.sleep(.2)
    raise MigrationError('A browser is still running. Close both browsers manually and retry.')


def summary_lines(result):
    labels = {'logins': 'Passwords', 'forms': 'Autofill', 'history': 'History visits', 'archive': 'Archived visits (included in history)'}
    lines = [f'{labels.get(k, k.title())}: {v:,}' for k, v in result.counts.items()]
    lines += [(f'Approximated themes: {v:,}' if k == 'themeApproximations' else f'Skipped {k}: {v:,}')
              for k, v in result.skipped.items() if v]
    return lines + result.warnings


def review(ui, result, dry_run=False):
    metrics = [(label, result.counts.get(key, 0)) for key, label in
               [('profiles', 'Profiles'), ('spaces', 'Spaces'), ('tabs', 'Tabs'),
                ('history', 'History visits'), ('logins', 'Passwords'), ('cookies', 'Cookies')]]
    while True:
        choice = ui.choose('Review before replacing Zen', [
            'Existing Zen data will be replaced.', 'A full backup will be kept for recovery.'],
            [('Continue migration', 'confirm'), ('View migration details & limitations', 'details'),
             ('Back', 'back')], stage=3, metrics=metrics)
        if choice != 'details': return choice
        rows = [('Back', 'back')] + [(line, 'detail') for line in summary_lines(result)]
        while ui.choose('Scan details', ['Encrypted record counts are provisional until transfer.'], rows, stage=3) == 'detail': pass


def open_backup_folder(ui, backup, dry_run):
    if dry_run: return
    path = Path(backup).expanduser().resolve()
    try:
        if not path.is_dir(): raise OSError('Backup folder is missing')
        result = subprocess.run(['/usr/bin/open', str(path)], capture_output=True, timeout=15)
        if result.returncode: raise OSError('Finder could not open the folder')
    except (OSError, subprocess.TimeoutExpired):
        ui.choose('Could not open backup folder', ['Open this location in Finder:', str(path)], [('Back', 'back')], stage=5)


def recovery_screen(args, ui, dry_run=False, run=None):
    from .recovery import Recovery, DemoRecovery
    from .cli import closed
    if dry_run:
        backend = DemoRecovery()
    else:
        zen_apps = [args.zen_app] if args.zen_app else applications('zen')
        if not zen_apps: raise MigrationError('Zen not found. Specify --zen-app to restore a backup.')
        backend = Recovery(args.runtime, args.zen_root, lambda: closed(zen_apps))
    while True:
        entries, unavailable = backend.backups()
        if run: entries = [entry for entry in entries if entry['run'] == run]
        lines = ['Choose the backup created before a migration.']
        if unavailable: lines += [f'{unavailable} journals could not be used. Their files were kept.']
        if not entries:
            ui.choose('No backups available', lines + ['No restorable backup was found in the migration directory.'], [('Back', 'back')], stage=1)
            return False
        selected = ui.choose('Restore from backup', lines,
            [('Back', 'back')] + [(entry['run'] + ' · ' + entry['phase'], entry) for entry in entries], stage=1)
        if not isinstance(selected, dict): return False
        try:
            if not dry_run:
                try: closed(zen_apps)
                except MigrationError:
                    action = ui.choose('Close Zen before restoring', ['Zen must be closed to restore a consistent backup.'],
                        [('Back', 'back'), ('Continue: close Zen', 'close')], stage=1)
                    if action != 'close': continue
                    result = subprocess.run(['/usr/bin/osascript', '-e',
                        f'if application id "{BUNDLE_IDS["zen"]}" is running then tell application id "{BUNDLE_IDS["zen"]}" to quit'],
                        capture_output=True, timeout=30)
                    if result.returncode: raise MigrationError('Could not close Zen. Close it manually and retry.')
                    for attempt in range(30):
                        try: closed(zen_apps); break
                        except MigrationError:
                            if attempt == 29: raise
                            time.sleep(.2)
            preview = ui.work('Checking backup', lambda emit: backend.preview(selected['run'], emit), stage=3, scene='restore')
            lines = ['Current Zen data will be replaced with this backup.', 'Your current Zen data will be retained separately.',
                     'Run: ' + preview['run'], 'Backup: ' + preview['backup']]
            if preview['changed']:
                lines = ['Zen has changed since this migration.', 'Restoring will undo those later changes in active Zen.'] + lines
            action = ui.choose('Confirm restore', lines,
                [('Cancel', 'cancel'), ('Restore despite later changes' if preview['changed'] else 'Restore backup', 'restore')], stage=3)
            if action != 'restore': continue
            result = ui.work('Restoring Zen', lambda emit: backend.restore(preview, emit), stage=4, scene='restore')
            lines = ['Zen has been restored. Your backup is still available.', 'Run: ' + result['run']]
            if result.get('retained'): lines += ['Previous Zen data: ' + result['retained']]
            ui.receipt = [] if dry_run else ['Zen restored.', 'Backup: ' + result['backup']] + lines[1:]
            while ui.choose('Restore complete.', lines,
                    [('Finish', 'finish'), ('Open backup folder', 'backup')], stage=5) == 'backup':
                open_backup_folder(ui, result['backup'], dry_run)
            return True
        except (MigrationError, subprocess.TimeoutExpired) as exc:
            message = str(exc) if isinstance(exc, MigrationError) else 'Closing Zen timed out. Close it manually and retry.'
            ui.choose('Restore stopped', [message, 'Keep the backup and its run journal.'], [('Back to backups', 'back')], stage=3)


def finished(ui, result, args, demo):
    lines = [f"{result['counts']['spaces']} Spaces · {result['counts']['tabs']} tabs · {result['counts']['logins']} passwords",
             'Backup: ' + result['backup'], 'Run: ' + result['run']]
    cookie_notice = []
    if result.get('skipped', {}).get('cookies', 0):
        cookie_notice = [f"Cookies: {result['counts']['cookies']} transferred · {result['skipped']['cookies']} skipped.",
                         'Some sites may require signing in again.']
    lines += cookie_notice
    theme_notice = []
    if result.get('skipped', {}).get('themes', 0):
        theme_notice = [f"Space themes: {result.get('counts', {}).get('themes', 0)} transferred · {result['skipped']['themes']} skipped.",
                        'Unsupported themes use the native Zen default.']
    lines += theme_notice
    lines += diagnostics.receipt()
    lines += ['Keep the backup and run ID for recovery.',
              'To restore later, run the installer again and choose Restore from backup.',
              'The temporary tool is removed when you finish.']
    ui.receipt = ([] if demo else
        ['ArcToZen migration completed.', *cookie_notice, *theme_notice, 'Backup: ' + result['backup'], 'Run: ' + result['run'],
         'To restore later, run the installer again and choose Restore from backup.'])
    while True:
        choice = ui.choose('All set.', lines,
            [('Finish & Open Zen', 'open'), ('Restore from backup', 'restore'),
             ('Finish & Open GitHub', 'finish')], stage=5)
        if choice == 'restore':
            if recovery_screen(args, ui, demo, run=result['run']): return 0
            continue
        if choice == 'open':
            if not demo:
                try:
                    opened = subprocess.run(['/usr/bin/open', '-a', str(args.zen_app)], capture_output=True, timeout=15)
                    if opened.returncode: raise OSError()
                except (OSError, subprocess.TimeoutExpired):
                    ui.choose('Migration succeeded', ['Zen could not be opened automatically. Open it manually.'], [('Finish', 'finish')], stage=5)
            break
        if choice == 'finish' and not demo:
            try:
                opened = subprocess.run(['/usr/bin/open', 'https://github.com/luntikius/arc-to-zen'], capture_output=True, timeout=15)
                if opened.returncode: raise OSError()
            except (OSError, subprocess.TimeoutExpired):
                ui.choose('Migration succeeded', ['GitHub could not be opened automatically.',
                    'https://github.com/luntikius/arc-to-zen'], [('Finish', 'finish')], stage=5)
        break
    if demo and getattr(args, 'demo_failure', None) == 'cleanup':
        ui.choose('Migration succeeded; cleanup needs attention', [
            'The temporary installation could not be removed.', 'Your browser data and recovery backups are intact.'], [('Finish', 'finish')], stage=5)
    return 0


def run(args):
    from .cli import closed, paths
    demo = bool(getattr(args, 'demo', False))
    dry_run = demo or bool(getattr(args, 'dry_run', False))
    if getattr(args, 'demo_failure', None) and not demo:
        raise MigrationError('--demo-failure requires --demo.')
    with Terminal(animation=not getattr(args, 'no_animation', False), demo=demo) as ui:
        ui.intro()
        if getattr(args, 'restore', False):
            recovery_screen(args, ui, dry_run)
            return 0
        while True:
            try:
                if demo:
                    from .demo import Demo
                    backend = Demo(getattr(args, 'demo_failure', None))
                    browsers = [dict(kind='arc', version='1.170.0', path='/Applications/Arc.app', state='closed'),
                                dict(kind='zen', version='1.23.4b', path='/Applications/Zen.app', state='closed')]
                else:
                    try:
                        if not detect(args, ui): return 0
                    except MigrationError as exc:
                        action = ui.choose('Browser setup needs attention', [str(exc)],
                            [('Restore from backup', 'restore'), ('Exit', 'exit')], stage=1)
                        if action == 'restore': recovery_screen(args, ui, dry_run)
                        return 0
                    paths(args)
                    browsers = []
                    for kind in ('arc', 'zen'):
                        app = getattr(args, kind + '_app')
                        try: closed([app]); state = 'closed'
                        except MigrationError: state = 'running / needs attention'
                        browsers.append(dict(kind=kind, version=app_version(app), path=str(app), state=state))
                action = ui.choose('Your browsers are ready', [],
                    [('Continue to migration options', 'continue'), ('Restore from backup', 'restore'), ('Exit', 'cancel')], browsers=browsers, stage=1)
                if action == 'restore':
                    if recovery_screen(args, ui, dry_run): return 0
                    continue
                if action != 'continue': return 0
                if not demo and not ensure_closed(args, ui, stage=1): continue
                def inventory(emit):
                    emit('Discovering your profiles and Spaces…')
                    return backend.inventory(emit) if demo else catalog(args.arc_root)
                data = ui.work('Scanning Arc', inventory, stage=2, scene='scan')
                selection = Selection.from_args(args, data)
                mode = ui.choose('How would you like to migrate?',
                    ['Bring everything supported, or choose what comes with you.',
                     f"Found {len(data['profiles'])} profiles and {len(data['spaces'])} Spaces."],
                    [('Continue with full migration', 'full'), ('Select my options', 'select'), ('Back', 'back')])
                if mode == 'cancel': return 0
                if mode == 'back': continue
                while True:
                    if mode == 'select':
                        action, selection = selection_screen(ui, data, selection)
                        if action == 'cancel': return 0
                        if action == 'back': break
                    if not demo and not ensure_closed(args, ui, stage=3): continue
                    scanned = ui.work('Preparing migration', lambda emit: backend.scan(selection, emit) if demo else
                        scan(args, selection, emit, probe=True), stage=3, scene='scan')
                    choice = review(ui, scanned, dry_run)
                    if choice == 'cancel': return 0
                    if choice != 'confirm':
                        mode = 'select'
                        continue
                    if dry_run:
                        if not demo:
                            from .demo import Demo
                            backend = Demo()
                        result = ui.work('Moving your browser',
                            lambda emit: backend.transfer(scanned, emit), stage=4, scene='transfer')
                    else:
                        result = ui.work('Moving your browser', lambda emit: transfer(args, scanned,
                            lambda: closed([args.arc_app, args.zen_app]), emit), stage=4, scene='transfer')
                    return finished(ui, result, args, dry_run)
            except (MigrationError, KeyboardInterrupt) as exc:
                diagnostics.error('command', exc)
                message = str(exc) if isinstance(exc, MigrationError) else 'Cancelled.'
                ui.receipt = ['ArcToZen did not complete.', message, *diagnostics.receipt()]
                choice = ui.choose('Migration stopped', [message,
                    'No success is reported. Keep any backup and recovery run ID shown above.'],
                    [('Exit', 'exit'), ('Continue from browser setup', 'retry')], stage=4)
                if choice != 'retry': return 1
                ui.receipt = []
            except Exception as exc:
                diagnostics.error('command', exc)
                ui.choose('Operation stopped', ['An unexpected error occurred; browser data was not logged.',
                    'Check the saved run journal before retrying a transfer.'], [('Exit', 'exit')], stage=4)
                return 1
