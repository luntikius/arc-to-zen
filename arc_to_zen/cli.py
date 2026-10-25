from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .common import MigrationError, app_version, database, safe
from .source import arc_container, discover
from .transaction import Transaction, operation_lock
from .platforms import resolve_apps, preflight
from . import diagnostics, __version__


def closed(apps):
    import psutil
    try:
        for process in psutil.process_iter(["pid", "name"]):
            if process.pid == os.getpid():
                continue
            name = (process.info.get("name") or "").lower()
            if not any(x in name for x in ("arc", "zen")):
                continue
            try:
                exe = process.exe()
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                raise MigrationError("Cannot check browser processes; close both browsers and run from Terminal.") from None
            if any(exe.startswith(str(app) + "/") for app in apps):
                raise MigrationError("Close Arc and Zen completely before migration or rollback.")
    except psutil.AccessDenied:
        raise MigrationError("Cannot enumerate browser processes.") from None


def compatibility(args):
    arc, zen = app_version(args.arc_app), app_version(args.zen_app)
    return arc, zen


def paths(args):
    values = [args.arc_root, args.zen_root, args.runtime]
    if any(p.is_symlink() for p in values):
        raise MigrationError("Data roots and runtime cannot be symlinks.")
    values = [p.resolve() for p in values]
    for i, p in enumerate(values):
        for q in values[i + 1:]:
            if p.is_relative_to(q) or q.is_relative_to(p):
                raise MigrationError("Arc, Zen and migration runtime paths must not overlap.")


def inspect(args):
    arc, zen = compatibility(args)
    profiles = discover(args.arc_root)
    c = arc_container(args.arc_root)
    unavailable = False
    print(f"Arc {safe(arc)} → Zen {safe(zen)}; {len(profiles)} profiles; read-only inspection.")
    from .common import pairs, profile_key
    spaces = [s for _, s in pairs(c["spaces"])]
    for key, p in profiles.items():
        counts = {}
        for filename, table in (("History", "visits"), ("Login Data", "logins"), ("Cookies", "cookies")):
            path = p.path / filename
            if filename == "Cookies" and (p.path / "Network/Cookies").exists():
                path = p.path / "Network/Cookies"
            try:
                with database(path) as db:
                    counts[filename] = db.execute("SELECT count(*) FROM " + table).fetchone()[0]
            except MigrationError:
                counts[filename] = "unavailable (locked or invalid)"
                unavailable = True
        print(f"  {safe(p.name)}: spaces={sum(profile_key(s['profile']) == key for s in spaces)}, " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print("Replacement requires --replace. Existing Zen will be backed up. Keychain was not accessed.")
    return 2 if unavailable else 0


def migrate(args):
    from .selection import Selection, catalog
    from .transfer import scan, transfer
    selection = Selection.from_args(args, catalog(args.arc_root))
    if args.dry_run:
        summary = scan(args, selection)
        print("Read-only scan:", summary.counts, "Skipped:", summary.skipped)
        return 0
    if not args.replace:
        raise MigrationError("Use migrate --replace to explicitly request replacement, or --dry-run to inspect.")
    check_closed = lambda: closed([args.arc_app, args.zen_app])
    check_closed()
    summary = scan(args, selection, probe=True)
    def progress(stage, done=None, total=None):
        print(stage + (f" ({done}/{total})" if total is not None else "…"), flush=True)
    result = transfer(args, summary, check_closed, progress)
    print(f"Migration complete. Run: {result['run']}. Backup: {safe(result['backup'])}")
    if result['skipped'].get('cookies', 0):
        print(f"Cookies: {result['counts']['cookies']} transferred; {result['skipped']['cookies']} skipped. Some sites may require signing in again.")
    if result['skipped'].get('themes', 0):
        print(f"Space themes: {result['counts']['themes']} transferred; {result['skipped']['themes']} skipped. Unsupported themes use the native Zen default.")
    return 0



def parser():
    home = Path.home() / "Library/Application Support"
    p = argparse.ArgumentParser(description="Local Arc → Zen migration. Default operations are read-only.")
    p.add_argument('--diagnostics', action='store_true', help='Keep a private, sanitized diagnostic log after cleanup')
    for name, default in (("arc-root", home / "Arc"), ("zen-root", home / "zen"),
                          ("runtime", home / "ArcToZenMigration"), ("arc-app", None),
                          ("zen-app", None)):
        p.add_argument("--" + name, type=lambda s: Path(s).expanduser().absolute(), default=default)
    commands = p.add_subparsers(dest="command")
    w = commands.add_parser("wizard")
    w.add_argument("--no-animation", action="store_true")
    w.add_argument("--demo", action="store_true", help="Synthetic dry-run: never read or change real browser data")
    w.add_argument("--demo-failure", choices=("scan", "transfer", "cleanup"), help="Exercise a synthetic failure in --demo")
    w.add_argument("--dry-run", action="store_true", help="Scan and preview only; never replace Zen")
    w.add_argument("--restore", action="store_true", help="Open backup recovery without requiring Arc")
    commands.add_parser("inspect")
    m = commands.add_parser("migrate")
    m.add_argument("--profiles", nargs="+", help="Arc profile directory IDs, e.g. Default")
    m.add_argument("--spaces", nargs="+", help="Arc Space IDs, or none")
    from .selection import CATEGORIES
    m.add_argument("--categories", nargs="+", choices=CATEGORIES)
    group = m.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--replace", action="store_true")
    commands.add_parser("verify").add_argument("--run", required=True)
    repair = commands.add_parser("repair-spaces")
    repair.add_argument("--run", required=True)
    repair.add_argument("--drop-empty-spaces", action="store_true", help="Omit Spaces without tabs or folders during repair")
    appearance = commands.add_parser("repair-appearance")
    appearance.add_argument("--run", required=True)
    appearance.add_argument("--dry-run", action="store_true")
    appearance.add_argument("--reset-theme", metavar="SPACE_UUID",
                            help="Reset only this Space to native Zen defaults, without importing Arc appearance")
    commands.add_parser("repair-registration").add_argument("--run", required=True)
    r = commands.add_parser("rollback")
    r.add_argument("--run", required=True)
    r.add_argument("--force", action="store_true")
    return p


def wizard(args):
    from .wizard import run
    return run(args)


def _main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    try:
        with diagnostics.recording(args.runtime, args.diagnostics) as recorder:
            diagnostics.emit('invocation', version=__version__,
                dry_run=bool(getattr(args, 'dry_run', False) or getattr(args, 'demo', False)), demo=bool(getattr(args, 'demo', False)))
            status = 1
            try:
                status = _dispatch(args)
                return status
            finally:
                diagnostics.emit('session_end', exit_code=status,
                    status='cancelled' if status == 130 else 'ok' if status == 0 else 'failed')
                if recorder:
                    recorder.close()
                    for line in diagnostics.receipt(): print(safe(line), file=sys.stderr)
    except MigrationError as exc:
        print(f'Error: {safe(exc)}', file=sys.stderr)
        return 1


def _dispatch(args):
    try:
        if args.command in (None, "wizard"):
            return wizard(args)
        resolve_apps(args)
        paths(args)
        if args.command == "inspect":
            return inspect(args)
        if args.command == "migrate":
            return migrate(args)
        if args.command == "repair-appearance":
            from .repair_appearance import repair_appearance
            if not args.dry_run:
                closed([args.arc_app, args.zen_app])
                preflight(args)
            repair_appearance(args, lambda: closed([args.arc_app, args.zen_app]))
            return 0
        closed([args.arc_app, args.zen_app])
        transaction = Transaction(args.runtime, args.run)
        if args.command == "repair-spaces":
            from .repair import repair_spaces
            preflight(args)
            repair_spaces(args, lambda: closed([args.arc_app, args.zen_app]))
        elif args.command == "verify":
            transaction.verify()
            print("Installed profiles and backup verified.")
        elif args.command == "repair-registration":
            compatibility(args)
            with operation_lock(args.runtime):
                transaction.repair_registration(lambda: closed([args.arc_app, args.zen_app]), args.zen_app)
            print("Profile registration repaired; all previous data retained in the run directory.")
        else:
            with operation_lock(args.runtime):
                transaction.rollback(args.force, lambda: closed([args.arc_app, args.zen_app]))
            print("Rollback complete; replaced data retained in the run directory.")
        return 0
    except MigrationError as exc:
        diagnostics.error('command', exc)
        print(f"Error: {safe(exc)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt as exc:
        diagnostics.error('command', exc)
        print("Interrupted. Use rollback --run to reconcile the saved run.", file=sys.stderr)
        return 130
    except Exception as exc:
        diagnostics.error('command', exc)
        # DB/browser exceptions can include source URLs, usernames or values.
        print("Error: operation failed; browser data was not logged. Check the run journal phase before retrying.", file=sys.stderr)
        return 1


def main(argv=None):
    import signal
    def interrupted(signum, frame):
        # A second signal must not interrupt transaction/process cleanup.
        for sig in signals:
            signal.signal(sig, signal.SIG_IGN)
        raise KeyboardInterrupt
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {sig: signal.getsignal(sig) for sig in signals}
    try:
        for sig in signals:
            signal.signal(sig, interrupted)
        return _main(argv)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())
