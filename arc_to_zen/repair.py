"""Repair old migrations using current Zen snapshots, never stale Arc data."""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from .common import validate_staging_tree, MigrationError, database, private_tree, read_json
from .merge import merge_profiles
from .source import Profile, identifier
from .transaction import Transaction, marker, operation_lock, tree_digest, validate_target
from .zen import Gecko, comparable_sidebar, stage_profile


def export_profile(app, path, name, temporary):
    snapshot = temporary / identifier("snapshot", str(path))
    shutil.copytree(path, snapshot, symlinks=True)
    validate_staging_tree(snapshot)
    private_tree(snapshot)
    for lock in ("parent.lock", ".parentlock", "lock"):
        (snapshot / lock).unlink(missing_ok=True)
    # A copied storeID makes Gecko recognize the copy as an existing registered
    # profile and relocate that registration. Detach it BEFORE starting Gecko.
    prefs = snapshot / "prefs.js"
    if prefs.exists():
        prefs.write_text("\n".join(line for line in prefs.read_text().splitlines()
            if '\"toolkit.profiles.' not in line and '\"browser.profiles.' not in line) + "\n")
    p = Profile(str(path), name, path)
    with Gecko(app, snapshot) as g:
        exported = g.call("export", None)
    for kind in ("history", "logins", "cookies", "bookmarks"):
        setattr(p, kind, exported[kind])
    windows = exported["state"].get("windows", [])
    if not windows:
        raise MigrationError("Current Zen session has no restorable window.")
    # Zen synchronizes its sidebar across windows. Retain the union of live tabs;
    # identical sync identities are the same tab, never deduplicate by URL.
    p.sidebar = {k: [] for k in ("tabs", "spaces", "folders", "groups", "splitViewData")}
    for kind, field in (("tabs", "zenSyncId"), ("spaces", "uuid"), ("folders", "id"),
                        ("groups", "id"), ("splitViewData", "groupId")):
        seen = {}
        for window in windows:
            if kind not in window:
                raise MigrationError("Unknown current Zen session schema.")
            for obj in window[kind]:
                if kind == "tabs" and obj.get("zenIsEmpty") and not obj.get("groupId"):
                    continue
                key = obj.get(field)
                if not key:
                    raise MigrationError("Unknown current Zen structural identity.")
                if key in seen and seen[key] != obj:
                    # Two synchronized windows can retain a different selected
                    # navigation; do not silently choose one user's state.
                    if kind == "tabs" and seen[key].get("entries") != obj.get("entries"):
                        raise MigrationError("Conflicting tab state across Zen windows; close extra windows first.")
                elif key not in seen:
                    seen[key] = obj
        p.sidebar[kind] = list(seen.values())
    p.sidebar["activeZenSpace"] = windows[0].get("activeZenSpace")
    forms = snapshot / "formhistory.sqlite"
    if forms.exists():
        with database(forms) as db:
            p.forms = [dict(r) for r in db.execute("SELECT fieldname,value,timesUsed,firstUsed,lastUsed FROM moz_formhistory")]
    # Native encrypted payment storage cannot be copied between NSS key stores.
    # Until an export adapter exists, fail rather than erase user-added records.
    autofill = snapshot / "autofill-profiles.json"
    if autofill.exists():
        values = read_json(autofill)
        p.addresses = [{k: v for k, v in r.items() if k not in ("guid", "version")} for r in values.get("addresses", [])]
        if values.get("creditCards"):
            raise MigrationError("Current Zen contains payment cards; repair needs a compatible native card export adapter.")
    return p


def repair_spaces(args, check_closed):
    parent = Transaction(args.runtime, args.run)
    old = parent.load()
    target = Path(old["target"])
    if target.resolve() != args.zen_root.resolve():
        raise MigrationError("Run belongs to another Zen data root.")
    check_closed()
    validate_target(target)
    with operation_lock(args.runtime):
        current = marker(target)
        if current:
            active = Transaction(args.runtime, current).load()
            if active.get("repair_of") == args.run and active["phase"] == "installed":
                print(f"Spaces already repaired by run {current}; no changes made.")
                return current
        if current != args.run or old["phase"] != "installed":
            raise MigrationError("Repair requires the selected original migration to be installed.")
        before = tree_digest(target)
        tx = Transaction.create(args.runtime, target)
        d = tx.load(); d["repair_of"] = args.run; tx.save(d)
        print(f"Run {tx.directory.name}: reading current Zen snapshots.", flush=True)
        try:
            profiles = []
            with tempfile.TemporaryDirectory(prefix="snapshots-", dir=tx.directory) as temp:
                for folder, meta in old["profiles"].items():
                    path = target / "Profiles" / folder
                    if not path.resolve().is_relative_to(target.resolve()):
                        raise MigrationError("Invalid source profile path.")
                    profiles.append(export_profile(args.zen_app, path, meta["name"], Path(temp)))
                if getattr(args, "drop_empty_spaces", False):
                    for p in profiles:
                        occupied = {t.get("zenWorkspace") for t in p.sidebar["tabs"] if not t.get("zenIsEmpty")}
                        occupied.update(f.get("workspaceId") for f in p.sidebar["folders"])
                        p.sidebar["spaces"] = [s for s in p.sidebar["spaces"] if s["uuid"] in occupied]
                combined = merge_profiles(profiles)
                folder = identifier("profile", "combined")
                meta = stage_profile(args.zen_app, tx.stage / "Profiles" / folder, combined)
                meta.pop("sidebar", None)
                meta["sidebar_hash"] = hashlib.sha256(json.dumps(comparable_sidebar(combined.sidebar), sort_keys=True).encode()).hexdigest()
                print(f"Verified combined profile: {meta['counts']}; containers={len(combined.containers)}.", flush=True)
            if tree_digest(target) != before:
                raise MigrationError("Current Zen changed while preparing the repair.")
            tx.install({folder: meta}, check_closed, expected_before=before)
            print(f"Spaces repaired. Run: {tx.directory.name}. Full previous Zen retained in backup.")
            return tx.directory.name
        except BaseException:
            d = tx.load()
            if d["phase"] == "preparing":
                d["phase"] = "failed"; tx.save(d)
            raise
