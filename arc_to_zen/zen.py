from __future__ import annotations

import json
import os
import socket
import subprocess
import shutil
import tempfile
import time
from pathlib import Path

from .common import MigrationError, private_opener, database, validate_staging_tree
from . import diagnostics

MAGIC = b"mozLz40\x00"


def read_lz4(path):
    import lz4.block
    try:
        raw = path.read_bytes()
        if raw[:8] != MAGIC:
            raise ValueError()
        return json.loads(lz4.block.decompress(raw[8:]))
    except (OSError, ValueError, lz4.block.LZ4BlockError) as exc:
        raise MigrationError("Invalid Zen session file.") from exc


def write_lz4(path, value):
    import lz4.block
    raw = json.dumps(value, ensure_ascii=False).encode()
    with open(path, "wb", opener=private_opener) as f:
        f.write(MAGIC + lz4.block.compress(raw, store_size=True))


def session_documents(sidebar, base=None, cookies=None):
    base = base or {"version": ["sessionrestore", 1]}
    w = dict((base.get("windows") or [{}])[0])
    w.update(sidebar)
    w["selected"] = max(1, next((i + 1 for i, t in enumerate(w["tabs"]) if not t.get("zenIsEmpty")), 1))
    w["_closedTabs"] = []
    state = {**base, "windows": [w], "_closedWindows": [], "savedGroups": [], "selectedWindow": 1,
             "session": {"lastUpdate": int(time.time() * 1000), "startTime": int(time.time() * 1000), "recentCrashes": 0}}
    if cookies is not None:
        state["cookies"] = cookies
    zen = {k: sidebar[k] for k in ("tabs", "spaces", "folders", "groups", "splitViewData")}
    zen["lastCollected"] = int(time.time() * 1000)
    return state, zen


class Gecko:
    """A short-lived, offline process. It never attaches to a user's window."""
    def __init__(self, app: Path, profile: Path):
        self.app, self.profile = app, profile
        self.process = self.client = None
        self.temporary_prefs = set()

    def __enter__(self):
        from marionette_driver.marionette import Marionette
        self.profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        validate_staging_tree(self.profile)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        prefs = {
            "marionette.port": port, "marionette.force-local": True,
            "browser.shell.checkDefaultBrowser": False, "browser.startup.page": 3,
            "browser.sessionstore.resume_from_crash": True, "browser.sessionstore.restore_on_demand": True,
            "browser.sessionstore.restore_pinned_tabs_on_demand": True, "browser.sessionstore.privacy_level": 0,
            "browser.sessionstore.max_serialize_back": -1, "browser.sessionstore.max_serialize_forward": -1,
            "browser.sessionhistory.max_entries": 1000000,
            "browser.history_expire_days_min": 365000, "places.history.expiration.max_pages": 100000000,
            "browser.aboutwelcome.enabled": False, "browser.startup.homepage_override.mstone": "ignore",
            "zen.welcome-screen.seen": True, "zen.welcome-screen.version": __import__("arc_to_zen.common", fromlist=["app_version"]).app_version(self.app),
            "zen.workspaces.continue-where-left-off": True, "zen.window-sync.sync-only-pinned-tabs": False,
            "app.update.auto": False, "app.update.enabled": False,
            "browser.profiles.enabled": False,
            "toolkit.telemetry.enabled": False, "datareporting.healthreport.uploadEnabled": False,
            "datareporting.policy.dataSubmissionEnabled": False, "network.manage-offline-status": False,
            "network.offline": True,
            "privacy.userContext.enabled": True, "privacy.userContext.ui.enabled": True,
            "zen.workspaces.separate-essentials": True,
        }
        self.temporary_prefs = set(prefs) - {
            "browser.startup.page", "browser.sessionstore.restore_on_demand",
            "browser.sessionstore.restore_pinned_tabs_on_demand", "browser.sessionstore.privacy_level",
            "browser.sessionstore.max_serialize_back", "browser.sessionstore.max_serialize_forward",
            "browser.sessionhistory.max_entries", "zen.window-sync.sync-only-pinned-tabs",
            "zen.workspaces.continue-where-left-off",
            "zen.welcome-screen.seen", "zen.welcome-screen.version",
            "privacy.userContext.enabled", "privacy.userContext.ui.enabled", "zen.workspaces.separate-essentials",
        }
        with open(self.profile / "user.js", "w", opener=private_opener) as f:
            for k, v in prefs.items():
                f.write(f"user_pref({json.dumps(k)}, {json.dumps(v)});\n")
        env = {**os.environ, "MOZ_REMOTE_ALLOW_SYSTEM_ACCESS": "1", "MOZ_CRASHREPORTER_DISABLE": "1"}
        # Logs may contain URLs or browser diagnostics; never persist them.
        try:
            self.process = subprocess.Popen([str(self.app / "Contents/MacOS/zen"), "--headless", "--no-remote", "--offline",
                "--profile", str(self.profile), "--marionette", "--remote-allow-system-access"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            self.client = Marionette(host="127.0.0.1", port=port, socket_timeout=120)
            self._start_session()
            self.client.set_context("chrome")
            self.client.timeout.script = 120
            identity = self.call("identity", None)
            if Path(identity["path"]).resolve() != self.profile.resolve() or identity["pid"] != self.process.pid:
                raise MigrationError("Staging browser identity mismatch.")
            if not identity["startupOffline"]:
                raise MigrationError("Staging browser was not offline from startup.")
            return self
        except BaseException as exc:
            self.close()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise MigrationError("Cannot start isolated Zen staging process.") from None

    def _start_session(self):
        from marionette_driver.errors import SessionNotCreatedException
        for attempt in range(3):
            try:
                self.client.start_session(timeout=60)
                return
            except SessionNotCreatedException as exc:
                # Zen can replace its selected tab while Marionette is waiting
                # for the initial navigation. Retry only that observed race,
                # before identity verification or any data operation.
                if ('contentBrowser is null' not in exc.message or '"focus"' not in exc.message
                        or attempt == 2):
                    raise
                diagnostics.emit('phase', operation='startup', status='retrying', reason='startup_focus')
                # Closing the transport makes Marionette's server onClosed()
                # delete its partially created session; do not replay writes.
                self.client.delete_session(send_request=False)
                time.sleep(.5)

    def call(self, op, data):
        script = Path(__file__).with_name("gecko.js").read_text()
        started = time.monotonic()
        diagnostics.emit('native_call', operation=op, status='started')
        try:
            result = self.client.execute_async_script(script, script_args=[op, data], new_sandbox=False)
        except Exception as exc:
            diagnostics.error(op, exc)
            raise MigrationError(f"Zen operation {op} failed.") from None
        if not result or not result.get("ok"):
            diagnostics.emit('native_call', operation=op, status='failed', reason=(result or {}).get('reason'))
            raise MigrationError(f"Zen operation {op} failed: {result.get('reason', 'unsupported data or API') if result else 'no response'}.")
        diagnostics.emit('native_call', operation=op, status='ok', elapsed_ms=round((time.monotonic() - started) * 1000))
        return result["value"]

    @diagnostics.traced('shutdown')
    def close(self):
        forced = False
        try:
            if self.client:
                try:
                    self.client.execute_script("Services.startup.quit(Ci.nsIAppStartup.eAttemptQuit);")
                except Exception:
                    pass
            if self.process:
                try:
                    self.process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    forced = True
        finally:
            # Also runs after cancellation during startup or shutdown. Do not
            # discard the profile while an owned process can still write it.
            try:
                if self.process and self.process.poll() is None:
                    forced = True
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()
            finally:
                if self.client:
                    try:
                        self.client.cleanup()
                    except Exception:
                        pass
                self.client = self.process = None
        if forced:
            raise MigrationError("Staging Zen did not shut down cleanly; replacement stopped.")

    def __exit__(self, *_):
        self.close()


def batches(values, size=500):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def prepare_profile_group(app, root, profiles):
    """Use Zen's datastore schema in a disposable browser, then install its group."""
    entries = [{"path": "Profiles/" + folder, "name": meta["name"]}
               for folder, meta in profiles.items()]
    with tempfile.TemporaryDirectory(prefix="arc-to-zen-group-", dir=root.parent) as temp:
        builder = Path(temp) / "builder"
        with Gecko(app, builder) as browser:
            result = browser.call("profileGroup", entries)
            if result["profiles"] != entries or Path(result["path"]).parent != builder:
                raise MigrationError("Zen profile group verification failed.")
        group_id = result["id"]
        if len(group_id) != 8 or any(c not in "0123456789abcdef" for c in group_id):
            raise MigrationError("Unexpected Zen profile group ID.")
        groups = root / "Profile Groups"
        groups.mkdir(mode=0o700, exist_ok=True)
        shutil.copyfile(Path(result["path"]), groups / (group_id + ".sqlite"))
    for folder, meta in profiles.items():
        prefs = root / "Profiles" / folder / "prefs.js"
        values = {"browser.profiles.enabled": True, "browser.profiles.created": True,
                  "toolkit.profiles.storeID": group_id}
        lines = [line for line in prefs.read_text().splitlines()
                 if not any(json.dumps(k) + "," in line for k in values)]
        lines += [f"user_pref({json.dumps(k)}, {json.dumps(v)});" for k, v in values.items()]
        prefs.write_text("\n".join(lines) + "\n")
        meta["store_id"] = group_id


def comparable_sidebar(sidebar):
    """Semantic comparison: IDs, ordering, structure, URL histories and pin state."""
    folders = {f["id"]: f for f in sidebar.get("folders", [])}
    def workspace(folder):
        # Native nested split groups inherit their workspace from the enclosing
        # folder; Zen serializes their own workspaceId as null.
        seen = set()
        while folder:
            if folder.get("workspaceId"):
                return folder["workspaceId"]
            if folder["id"] in seen:
                raise MigrationError("Cyclic Zen folder inheritance.")
            seen.add(folder["id"])
            folder = folders.get(folder.get("parentId"))
        return None
    return {
        "spaces": [(s["uuid"], s["name"], s.get("containerTabId", 0)) for s in sidebar.get("spaces", [])],
        "tabs": [(t["zenSyncId"], t.get("zenWorkspace"), t.get("groupId"), t.get("userContextId", 0),
                  [(e["url"], e.get("title", "")) for e in t.get("entries", [])], t.get("index", 1),
                  bool(t.get("pinned")), bool(t.get("zenEssential")),
                  t.get("_zenPinnedInitialState", {}).get("entry", {}).get("url"))
                 for t in sidebar.get("tabs", []) if not t.get("zenIsEmpty")],
        "folders": [(f["id"], f["name"], f.get("parentId"), workspace(f)) for f in sidebar.get("folders", [])],
        "splits": [(s["groupId"], s["gridType"], s["tabs"]) for s in sidebar.get("splitViewData", [])],
    }


def check_sidebar(expected, actual):
    if comparable_sidebar(expected) != comparable_sidebar(actual):
        raise MigrationError("Zen did not preserve the complete sidebar structure.")
    for tab in expected.get('tabs', []):
        if tab.get('zenDefaultUserContextId') in (True, 'true'):
            stored = next(t for t in actual['tabs'] if t.get('zenSyncId') == tab['zenSyncId'])
            if stored.get('zenDefaultUserContextId') not in (True, 'true'):
                raise MigrationError('Zen did not preserve the Space default container indicator.')
    for folder in expected.get("folders", []):
        stored = next(f for f in actual["folders"] if f["id"] == folder["id"])
        if (stored.get("userIcon") or None) != (folder.get("userIcon") or None):
            raise MigrationError("Zen did not preserve folder appearance.")
    for space in expected.get("spaces", []):
        stored = next(s for s in actual["spaces"] if s["uuid"] == space["uuid"])
        for field in ("theme", "icon"):
            if field in space and stored.get(field) != space[field]:
                raise MigrationError("Zen did not preserve workspace appearance.")


def stage_profile(app, path, p, emit=lambda *args: None):
    p.imported_space_count = len(p.sidebar["spaces"])
    emit("Starting isolated Zen")
    with Gecko(app, path) as g:
        g.call("capabilities", None)
        from .appearance import capabilities, resolve_sidebar
        resolve_sidebar(p.sidebar, capabilities(g), p.skipped)
        p.theme_count = sum(bool(s.get('theme')) for s in p.sidebar['spaces'])
        if any(not s.get('theme') for s in p.sidebar['spaces']):
            from copy import deepcopy
            default_theme = g.call('defaultTheme', None)
            for space in p.sidebar['spaces']:
                if not space.get('theme'):
                    space['theme'] = deepcopy(default_theme)
                    diagnostics.emit('theme', space=diagnostics.alias('space', space['uuid']), stage='mapping',
                        status='default', output_theme=default_theme)
        if p.containers:
            from .merge import remap_contexts
            mapping = g.call("containers", p.containers)
            remap_contexts(p, {int(k): v for k, v in mapping.items()})
        for space in p.sidebar['spaces']:
            diagnostics.emit('space', stage='mapping', space=diagnostics.alias('space', space['uuid']),
                container=diagnostics.alias('container', space['containerTabId']),
                profile=next((c.get('_diagnosticProfile', 0) for c in p.containers if c['id'] == space['containerTabId']), 0),
                tabs=sum(t.get('zenWorkspace') == space['uuid'] for t in p.sidebar['tabs']),
                matches=all(t.get('userContextId') == space['containerTabId'] for t in p.sidebar['tabs']
                    if t.get('zenWorkspace') == space['uuid']))
        for kind in ("history", "logins", "cookies", "bookmarks", "forms", "addresses", "cards"):
            accepted_history = []
            accepted_cookies = []
            accepted_optional = []
            completed = 0
            for batch in batches(getattr(p, kind)):
                emit("Transferring " + kind, completed, len(getattr(p, kind)))
                result = g.call(kind, batch)
                completed += len(batch)
                if kind == "history":
                    skipped = set(result["skipped"])
                    accepted = [r for i, r in enumerate(batch) if i not in skipped]
                    if result["inserted"] != len(accepted):
                        raise MigrationError("Incomplete history import.")
                    accepted_history.extend(accepted)
                    p.skipped["history"] += len(skipped)
                elif kind == "cookies":
                    if (not isinstance(result, dict) or not isinstance(result.get("skipped"), list)
                            or any(type(i) is not int or not 0 <= i < len(batch) for i in result["skipped"])):
                        raise MigrationError("Unknown cookie import result.")
                    skipped = set(result["skipped"])
                    if len(skipped) != len(result["skipped"]) or result.get("inserted") != len(batch) - len(skipped):
                        raise MigrationError("Incomplete cookie import.")
                    reasons = result.get('reasons')
                    if (not isinstance(reasons, dict) or set(reasons) != {str(i) for i in skipped}
                            or any(v not in diagnostics.REASONS for v in reasons.values())):
                        raise MigrationError('Unknown cookie rejection reason.')
                    for i in sorted(skipped):
                        diagnostics.cookie_skip(batch[i].get('_diagnosticId', 0), 'native', reasons[str(i)])
                    diagnostics.emit('cookie_batch', stage='native', accepted=result['inserted'], skipped=len(skipped))
                    accepted_cookies.extend(r for i, r in enumerate(batch) if i not in skipped)
                    p.skipped["cookies"] += len(skipped)
                elif isinstance(result, dict) and "unsupported" in result:
                    if result["unsupported"] != len(batch):
                        raise MigrationError("Unknown partial native import result.")
                    p.skipped[kind] += result["unsupported"]
                elif kind == "forms" and result != len(batch):
                    raise MigrationError(f"Incomplete {kind} import.")
                if kind in ("addresses", "cards") and not (isinstance(result, dict) and "unsupported" in result):
                    accepted_optional.extend(batch)
            if kind in ("addresses", "cards"):
                setattr(p, kind, accepted_optional)
            if kind == "history":
                p.history = accepted_history
            if kind == "cookies":
                p.cookies = accepted_cookies
                diagnostics.cookie_summary(len(p.cookies))
        result = g.call("verifyData", {"logins": p.logins, "cookies": p.cookies})
        if result["history"] != len(p.history):
            raise MigrationError("Incomplete history import.")
        base = g.call("state", None)
        if not p.sidebar['spaces']:
            # Keep Zen's own initial workspace, with no imported container.
            from copy import deepcopy
            initial = base['windows'][0]
            p.sidebar['spaces'] = deepcopy(initial['spaces'])
            p.sidebar['activeZenSpace'] = initial['activeZenSpace']
    # Gecko is closed before writing session files, so it cannot overwrite them.
    state, sidebar = session_documents(p.sidebar, base, [
        {"host": c["host"], "path": c["path"], "name": c["name"], "value": c["value"],
         "secure": c["secure"], "httponly": c["httpOnly"], "originAttributes": c["originAttributes"],
         "sameSite": c["sameSite"], "schemeMap": c["schemeMap"], "isPartitioned": bool(c["originAttributes"].get("partitionKey"))}
        for c in p.cookies if c["session"]])
    write_lz4(path / "sessionstore.jsonlz4", state)
    write_lz4(path / "zen-sessions.jsonlz4", sidebar)
    # Open, verify live structures and credentials, close, and open again.
    for restart in range(2):
        emit("Verifying restored Zen", restart + 1, 2)
        with Gecko(app, path) as g:
            live = g.call("state", None)
            if not live.get("windows"):
                raise MigrationError("Zen did not restore the staging window.")
            check_sidebar(p.sidebar, live["windows"][0])
            observed_themes = g.call("verifyAppearance", p.sidebar)
            for observed in observed_themes:
                diagnostics.emit('theme', stage='restart', restart=restart + 1,
                    space=diagnostics.alias('space', observed['uuid']), output_theme=observed['theme'], status='verified')
            if p.containers:
                g.call("verifyContainers", {"containers": p.containers, "spaces": p.sidebar["spaces"]})
                ui = g.call("verifyUI", p.sidebar["spaces"])
                for observed in ui:
                    diagnostics.emit('space', stage='restart', restart=restart + 1,
                        space=diagnostics.alias('space', observed['uuid']),
                        container=diagnostics.alias('container', observed['container']),
                        matches=True, indicator_hidden=observed['indicatorsHidden'],
                        hide_default_indicator=observed['hideDefaultIndicator'],
                        essentials_indicator_hidden=observed['essentialsIndicatorsHidden'], status='verified')
            result = g.call("verifyData", {"logins": p.logins, "cookies": p.cookies})
            if result["history"] != len(p.history):
                raise MigrationError("History changed after browser restart.")
            diagnostics.emit('verification', stage='restart', restart=restart + 1, accepted=len(p.cookies), status='verified')
    if p.forms:
        with database(path / "formhistory.sqlite") as db:
            forms = {(r[0], r[1]): r[2] for r in db.execute("SELECT fieldname,value,timesUsed FROM moz_formhistory")}
        for form in p.forms:
            if forms.get((form["fieldname"], form["value"]), 0) < form["timesUsed"]:
                raise MigrationError("Form history did not survive browser restart.")
    # Remove all migration-only preferences. Keep session restoration and on-demand loading.
    (path / "user.js").unlink(missing_ok=True)
    prefs = path / "prefs.js"
    lines = prefs.read_text().splitlines()
    remove = g.temporary_prefs
    prefs.write_text("\n".join(l for l in lines if not any(json.dumps(k) + "," in l for k in remove)
                               and '"marionette.' not in l and '"remote.' not in l) + "\n")
    for name in ("parent.lock", ".parentlock", "lock"):
        (path / name).unlink(missing_ok=True)
    from .appearance import appearance_digest
    return {"name": p.name, "appearance_hash": appearance_digest(read_lz4(path / "zen-sessions.jsonlz4")), "counts": {"tabs": sum(not t.get("zenIsEmpty", False) for t in p.sidebar["tabs"]),
        "spaces": len(p.sidebar["spaces"]), "history": len(p.history), "logins": result["logins"]},
        "sidebar": comparable_sidebar(p.sidebar)}
