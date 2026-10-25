"""macOS installation discovery and preflight without accessing secrets."""
from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
from pathlib import Path

from .common import MigrationError, safe
from . import diagnostics

BUNDLE_IDS = {"arc": "company.thebrowser.Browser", "zen": "app.zen-browser.zen"}


def applications(kind):
    bundle = BUNDLE_IDS[kind]
    candidates = list(Path('/Applications').glob('*.app')) + list((Path.home() / 'Applications').glob('*.app'))
    result = subprocess.run(['/usr/bin/mdfind', 'kMDItemCFBundleIdentifier == "' + bundle + '"'],
                            capture_output=True, text=True, check=False)
    if result.returncode == 0:
        candidates.extend(Path(p) for p in result.stdout.splitlines())
    found = set()
    for app in candidates:
        try:
            with open(app / 'Contents/Info.plist', 'rb') as f:
                info = plistlib.load(f)
            if info.get('CFBundleIdentifier') == bundle:
                found.add(app.resolve())
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
    return sorted(found)


def resolve_apps(args, choose=None):
    for kind in BUNDLE_IDS:
        given = getattr(args, kind + '_app')
        if given is not None:
            matches = [given]
        else:
            matches = applications(kind)
        if not matches:
            raise MigrationError(f'{kind.title()} not found; install it or specify --{kind}-app.')
        if len(matches) > 1:
            if choose is None:
                raise MigrationError(f'Multiple {kind.title()} installations; specify --{kind}-app.')
            print(f'{kind.title()} installations:')
            for i, app in enumerate(matches, 1): print(f'  {i}. {safe(app)}')
            try:
                index = int(choose('Choose installation number: ')) - 1
                if index < 0: raise ValueError()
                matches = [matches[index]]
            except (ValueError, IndexError):
                raise MigrationError('Invalid installation selection.') from None
        app = matches[0]
        try:
            with open(app / 'Contents/Info.plist', 'rb') as f: info = plistlib.load(f)
            if info['CFBundleIdentifier'] != BUNDLE_IDS[kind]: raise ValueError()
            executable = app / 'Contents/MacOS' / info['CFBundleExecutable']
            if not os.access(executable, os.X_OK): raise ValueError()
        except (OSError, KeyError, ValueError):
            raise MigrationError(f'Invalid {kind.title()} application bundle.') from None
        setattr(args, kind + '_app', app)
        diagnostics.emit('browser', browser=kind, version=str(info.get('CFBundleShortVersionString', '')))


def preflight(args):
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise MigrationError('This release requires macOS on Apple Silicon (native arm64 runtime).')
    parent = args.runtime
    while not parent.exists(): parent = parent.parent
    if args.zen_root.stat().st_dev != parent.stat().st_dev:
        raise MigrationError('Runtime and Zen must be on the same filesystem.')
    needed = 0
    for root in (args.zen_root, args.arc_root):
        for directory, _, files in os.walk(root, followlinks=False):
            for name in files:
                p = Path(directory) / name
                if not p.is_symlink(): needed += p.stat().st_size
    if shutil.disk_usage(parent).free < needed * 3 + 512 * 1024 * 1024:
        raise MigrationError('Not enough free space for staging, snapshots and a full backup.')
    if not os.access(parent, os.W_OK) or not os.access(args.zen_root.parent, os.W_OK):
        raise MigrationError('No write access to the runtime or Zen data directory.')
