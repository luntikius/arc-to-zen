from __future__ import annotations

import hashlib
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .common import (MigrationError, arc_us, chrome_us, compatible_url, database,
                     pairs, profile_key, read_json, table_exists)
from .sessions import latest
from . import diagnostics


@dataclass(repr=False)
class Profile:
    key: str
    name: str
    path: Path
    containers: list = field(default_factory=list)
    sidebar: dict = field(default_factory=dict)
    history: list = field(default_factory=list)
    logins: list = field(default_factory=list)
    cookies: list = field(default_factory=list)
    bookmarks: list = field(default_factory=list)
    forms: list = field(default_factory=list)
    addresses: list = field(default_factory=list)
    cards: list = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)
    archived_visits: int = 0
    theme_count: int | None = None
    imported_space_count: int | None = None


def identifier(kind, value):
    return "arc-" + kind + "-" + hashlib.sha256(str(value).encode()).hexdigest()[:24]


def origin(url):
    p = urlsplit(url)
    host = p.hostname.encode("idna").decode().lower()
    if ":" in host:
        host = "[" + host + "]"
    port = p.port
    if port and port != {"https": 443, "http": 80, "ftp": 21}.get(p.scheme):
        host += ":" + str(port)
    return f"{p.scheme}://{host}"


def discover(root: Path):
    user_data = root / "User Data"
    state = read_json(user_data / "Local State")
    cache = state.get("profile", {}).get("info_cache", {})
    keys = set(cache)
    for p in user_data.iterdir():
        if (p.name == "Default" or p.name.startswith("Profile ")) and (p / "Preferences").is_file():
            keys.add(p.name)
    result = {}
    for key in sorted(keys, key=lambda k: (k != "Default", k)):
        if Path(key).name != key or key in (".", ".."):
            raise MigrationError("Invalid profile path in Local State.")
        path = user_data / key
        if path.is_symlink() or not path.is_dir():
            raise MigrationError("Missing or symlinked Arc profile directory.")
        result[key] = Profile(key, cache.get(key, {}).get("name", key), path)
    if not result:
        raise MigrationError("No persistent Arc profiles found.")
    return result


def versioned(root, name):
    value = read_json(root / name)
    if value.get("version") != 1:
        raise MigrationError(f"Unsupported {name} schema.")
    return value


def arc_container(root):
    d = versioned(root, "StorableSidebar.json")
    containers = [x for x in d["sidebar"]["containers"] if "spaces" in x and "items" in x]
    if len(containers) != 1:
        raise MigrationError("Unknown Arc sidebar container layout.")
    return containers[0]


def workspace(space):
    result = {"uuid": identifier("space", space["id"]), "name": space.get("title") or "Arc",
              "containerTabId": 0, "hasCollapsedPinnedTabs": False}
    result["_arcAppearance"] = space.get("customInfo") or {}
    return result


def build_sidebars(root, profiles, space_ids=None, all_profile_keys=None):
    c = arc_container(root)
    spaces = dict(pairs(c["spaces"]))
    items = dict(pairs(c["items"]))
    for key, v in items.items():
        if not isinstance(v, dict) or v.get("id") != key or len(v.get("data", {})) != 1:
            raise MigrationError("Invalid Arc sidebar item.")
    space_profiles = {}
    restore = versioned(root, "StorableSessionRestorationData.json")
    mappings = {}
    for reference, tabid in pairs(restore["mapping"]["keysToValues"]):
        key = profile_key(reference["browser"]["contextID"]["profile"])
        mappings[(key, reference["itemId"])] = tabid
    windows = versioned(root, "StorableWindows.json")
    focused = windows.get("lastFocusedSpaceID")
    expanded = set()
    for win in windows.get("windows", []):
        expanded.update(k for k, v in pairs(win.get("expandedItems", [])) if "expanded" in v)
    for p in profiles.values():
        p.sidebar = {"tabs": [], "folders": [], "groups": [], "splitViewData": [], "spaces": []}
    navs = {k: latest(p.path / "Sessions") for k, p in profiles.items()}
    visited = set()

    def visit(itemid, p, wid, pinned=False, essential=False, parent=None, previous=None, stack=()):
        if itemid in stack or itemid in visited:
            raise MigrationError("Repeated or cyclic Arc sidebar reference.")
        if itemid not in items:
            raise MigrationError("Missing Arc sidebar child.")
        visited.add(itemid)
        item = items[itemid]
        typ, data = next(iter(item["data"].items()))
        sid = identifier("item", itemid)
        if typ == "tab":
            saved = data.get("savedURL")
            native = navs[p.key].get(mappings.get((p.key, itemid)))
            state = native.state() if native else None
            if not state:
                if not compatible_url(saved):
                    p.skipped["tabs"] += 1
                    return previous
                state = {"entries": [{"url": saved, "title": data.get("savedTitle") or item.get("title") or ""}], "index": 1}
            tab = {**state, "zenSyncId": sid, "zenWorkspace": wid, "pinned": pinned or essential,
                   "zenEssential": essential, "userContextId": 0, "hidden": False,
                   "attributes": {}, "lastAccessed": arc_us(data.get("timeLastActiveAt", item.get("createdAt", 0))) // 1000}
            if item.get("title"):
                tab["zenStaticLabel"] = item["title"]
            if data.get("savedMuteStatus") == "muteAudio":
                tab["muted"] = True
            if parent:
                tab["groupId"] = parent
            if (pinned or essential) and compatible_url(saved):
                tab["_zenPinnedInitialState"] = {"entry": {"url": saved, "title": data.get("savedTitle") or ""}, "image": None}
            p.sidebar["tabs"].append(tab)
            return {"type": "tab", "id": sid}
        if typ not in ("list", "splitView", "itemContainer"):
            p.skipped["sidebar"] += 1
            return previous
        children = item.get("childrenIds", [])
        is_folder = typ in ("list", "splitView")
        if is_folder:
            p.sidebar["groups"].append({"id": sid, "name": item.get("title") or "Arc folder", "color": "grey",
                                         "collapsed": False, "pinned": pinned or essential,
                                         "saveOnWindowClose": True})
            if typ == "list" or parent:
                p.sidebar["folders"].append({"id": sid, "name": item.get("title") or "Arc folder",
                    "pinned": pinned or essential, "essential": essential, "splitViewGroup": typ == "splitView",
                    "collapsed": itemid not in expanded, "saveOnWindowClose": True,
                    "parentId": parent, "prevSiblingInfo": previous, "workspaceId": wid,
                    "emptyTabIds": [], "userIcon": None, "_arcAppearance": data.get("customInfo") or {}})
            if typ == "list":
                emptyid = sid + "-empty"
                p.sidebar["tabs"].append({"entries": [{"url": "about:blank"}], "index": 1,
                    "zenSyncId": emptyid, "zenWorkspace": wid, "groupId": sid, "zenIsEmpty": True,
                    "pinned": pinned or essential, "zenEssential": essential, "userContextId": 0, "attributes": {}})
                p.sidebar["folders"][-1]["emptyTabIds"] = [emptyid]
        prev = None
        for child in children:
            prev = visit(child, p, wid, pinned, essential, sid if is_folder else parent, prev, stack + (itemid,))
        if typ == "splitView":
            tabs = [t for t in p.sidebar["tabs"] if t.get("groupId") == sid]
            if len(tabs) < 2 or essential:
                # Essentials cannot be split in this Zen version; retain their tabs.
                p.skipped["splitViews"] += 1
            else:
                factors = dict(pairs(data.get("itemWidthFactors", [])))
                weights = [float(factors.get(x, 1 / len(tabs))) for x in children]
                total = sum(weights)
                if total <= 0 or any(x <= 0 for x in weights):
                    raise MigrationError("Invalid Arc split proportions.")
                orientation = data.get("layoutOrientation", "horizontal")
                p.sidebar["splitViewData"].append({"groupId": sid,
                    "gridType": "vsep" if orientation == "horizontal" else "hsep",
                    "tabs": [t["zenSyncId"] for t in tabs],
                    "layoutTree": {"type": "node", "direction": "row" if orientation == "horizontal" else "column",
                        "sizeInParent": 100, "children": [{"type": "leaf", "tabId": t["zenSyncId"],
                        "sizeInParent": weights[i] / total * 100} for i, t in enumerate(tabs)]}})
        return {"type": "group", "id": sid} if is_folder else prev

    for spaceid, space in spaces.items():
        key = profile_key(space["profile"])
        if key not in (all_profile_keys if all_profile_keys is not None else profiles):
            raise MigrationError("Space refers to a missing Arc profile.")
        space_profiles[spaceid] = key
        if key not in profiles or (space_ids is not None and spaceid not in space_ids):
            continue
        p = profiles[key]
        w = workspace(space)
        p.sidebar["spaces"].append(w)
        if spaceid == focused:
            p.sidebar["activeZenSpace"] = w["uuid"]
        roots = pairs(space["containerIDs"])
        # Firefox requires pinned tabs before unpinned tabs.
        roots.sort(key=lambda r: r[0] != "pinned")
        for kind, itemid in roots:
            visit(itemid, p, w["uuid"], pinned=kind == "pinned")
    for reference, rootid in pairs(c.get("topAppsContainerIDs", [])):
        key = profile_key(reference)
        p = profiles.get(key)
        if p is None:
            if all_profile_keys is not None and key in all_profile_keys:
                continue
            raise MigrationError("Favorites refer to a missing Arc profile.")
        # Essentials belong to a container, not to a fabricated workspace.
        visit(rootid, p, None, pinned=True, essential=True)
    for p in profiles.values():
        if p.sidebar["spaces"]:
            p.sidebar.setdefault("activeZenSpace", p.sidebar["spaces"][0]["uuid"])
        p.sidebar["tabs"].sort(key=lambda t: (not t.get("zenEssential", False), not t.get("pinned", False)))
    return space_profiles


def read_history(p):
    with database(p.path / "History") as db:
        rows = db.execute("SELECT u.url,u.title,v.visit_time,v.transition FROM visits v JOIN urls u ON u.id=v.url ORDER BY v.visit_time,v.id")
        for r in rows:
            if not compatible_url(r["url"], history=True) or chrome_us(r["visit_time"]) <= 0:
                p.skipped["history"] += 1
                continue
            transition = {0: 1, 1: 2, 2: 3, 3: 8, 4: 8, 5: 1, 6: 1, 7: 1, 8: 9, 9: 2, 10: 1}.get(r["transition"] & 0xff, 1)
            p.history.append({"url": r["url"], "title": r["title"] or "", "date": chrome_us(r["visit_time"]), "transition": transition})


def add_archive(root, profiles, space_profiles, allow_history_fallback=True):
    entries = versioned(root, "StorableArchiveItems.json")
    by_url = {}
    seen = {}
    for key, p in profiles.items():
        seen[key] = {(r["url"], r["date"]) for r in p.history}
        for r in p.history:
            by_url.setdefault(r["url"], set()).add(key)
    skipped = 0
    for _, entry in pairs(entries["items"]):
        item = entry["sidebarItem"]
        tab = item.get("data", {}).get("tab", {})
        url = tab.get("savedURL")
        source = entry.get("source", {}).get("space", {}).get("_0")
        if isinstance(source, dict):
            source = source.get("id")
        key = space_profiles.get(source)
        if key is None and allow_history_fallback:
            candidates = by_url.get(url, set())
            key = next(iter(candidates)) if len(candidates) == 1 else None
        if key not in profiles or not compatible_url(url, history=True):
            skipped += 1
            continue
        when = tab.get("timeLastActiveAt") or entry.get("archivedAt")
        if when is None:
            skipped += 1
            continue
        date = arc_us(when)
        if (url, date) not in seen[key]:
            profiles[key].history.append({"url": url, "title": tab.get("savedTitle") or item.get("title") or "", "date": date, "transition": 1})
            seen[key].add((url, date))
    return skipped


def read_bookmarks(p):
    path = p.path / "Bookmarks"
    if not path.exists():
        return
    d = read_json(path)
    if d.get("version") != 1:
        raise MigrationError("Unsupported Chromium bookmarks schema.")
    def convert(item):
        if item.get("type") == "folder":
            return {"title": item.get("name", ""), "children": [v for x in item.get("children", []) if (v := convert(x)) is not None]}
        if compatible_url(item.get("url")):
            return {"title": item.get("name", ""), "url": item["url"]}
        p.skipped["bookmarks"] += 1
    p.bookmarks = [v for x in d["roots"].values() if (v := convert(x)) is not None]


def read_web_data(p, decrypt=None, categories=None):
    categories = {"forms", "addresses", "cards"} if categories is None else set(categories)
    if not categories:
        return
    path = p.path / "Web Data"
    if not path.exists():
        return
    with database(path) as db:
        if "forms" in categories and table_exists(db, "autofill"):
            p.forms = [{"fieldname": r["name"], "value": r["value"], "timesUsed": r["count"],
                        "firstUsed": int(r["date_created"]) * 1000000, "lastUsed": int(r["date_last_used"]) * 1000000}
                       for r in db.execute("SELECT * FROM autofill") if r["value"]]
        # Chromium 134+ local addresses use a typed token table; map only stable supported fields.
        if "addresses" in categories and table_exists(db, "addresses") and table_exists(db, "address_type_tokens"):
            columns = {r[1] for r in db.execute("PRAGMA table_info(address_type_tokens)")}
            if {"guid", "type", "value"} <= columns:
                fields = {7: "name", 9: "email", 14: "tel", 30: "organization", 33: "address-level2",
                          34: "address-level1", 35: "postal-code", 36: "country", 77: "street-address"}
                addresses = {}
                for r in db.execute("SELECT guid,type,value FROM address_type_tokens"):
                    if r["type"] in fields and r["value"]:
                        addresses.setdefault(r["guid"], {})[fields[r["type"]]] = r["value"]
                p.addresses = list(addresses.values())
            else:
                p.skipped["addresses"] += db.execute("SELECT count(*) FROM addresses").fetchone()[0]
        elif "addresses" in categories and table_exists(db, "autofill_profiles"):
            p.skipped["addresses"] += db.execute("SELECT count(*) FROM autofill_profiles").fetchone()[0]
        if "cards" in categories and table_exists(db, "credit_cards"):
            for r in db.execute("SELECT * FROM credit_cards"):
                if decrypt is None:
                    continue
                number = decrypt(bytes(r["card_number_encrypted"]))
                if number is None:
                    p.skipped["cards"] += 1
                    continue
                p.cards.append({"cc-name": r["name_on_card"], "cc-number": number.decode("utf-8"),
                    "cc-exp-month": r["expiration_month"], "cc-exp-year": r["expiration_year"]})


def read_secrets(p, decrypt):
    read_passwords(p, decrypt)
    read_cookies(p, decrypt)


def read_passwords(p, decrypt):
    imported = {}
    with database(p.path / "Login Data") as db:
        for r in db.execute("SELECT * FROM logins WHERE blacklisted_by_user=0"):
            if not compatible_url(r["origin_url"], history=True) or r["scheme"] not in (0, 1, 2):
                p.skipped["logins"] += 1
                continue
            if r["scheme"] == 0 and r["action_url"] and not compatible_url(r["action_url"], history=True):
                p.skipped["logins"] += 1
                continue
            plain = decrypt(bytes(r["password_value"]))
            if plain is None:
                raise MigrationError("Unknown password encryption; replacement stopped.")
            base = origin(r["origin_url"])
            login = {"origin": base, "formActionOrigin": (origin(r["action_url"]) if r["action_url"] else "") if r["scheme"] == 0 else None,
                "httpRealm": r["signon_realm"][len(base) + 1:] if r["scheme"] in (1, 2) else None,
                "username": r["username_value"], "password": plain.decode("utf-8"),
                "usernameElement": r["username_element"], "passwordElement": r["password_element"],
                "timeCreated": max(0, chrome_us(r["date_created"]) // 1000), "timesUsed": r["times_used"]}
            for source, dest in (("date_last_used", "timeLastUsed"), ("date_password_modified", "timePasswordChanged")):
                if source in r.keys() and r[source]:
                    login[dest] = max(0, chrome_us(r[source]) // 1000)
            identity = (login["origin"], login["formActionOrigin"], login["httpRealm"], login["username"])
            if identity in imported:
                p.skipped["logins"] += 1
                previous = imported[identity]
                if previous.get("timePasswordChanged", previous["timeCreated"]) > login.get("timePasswordChanged", login["timeCreated"]):
                    continue
            imported[identity] = login
    p.logins = list(imported.values())


def read_cookies(p, decrypt):
    cookie_path = p.path / "Network/Cookies"
    if not cookie_path.exists():
        cookie_path = p.path / "Cookies"
    with database(cookie_path) as db:
        version = int(db.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0])
        for r in db.execute("SELECT * FROM cookies"):
            diagnostic_id = diagnostics.cookie_read(p.key)
            cookie = cookie_record(dict(r), version, decrypt,
                on_skip=lambda reason: diagnostics.cookie_skip(diagnostic_id, 'source', reason))
            if cookie is None:
                p.skipped["cookies"] += 1
            else:
                cookie['_diagnosticId'] = diagnostic_id
                p.cookies.append(cookie)


def cookie_record(r, version, decrypt, now_s=None, on_skip=lambda reason: None):
    def reject(reason):
        on_skip(reason)
        return None
    # Gecko can store an ancestor-marked partition key, but equivalent request
    # isolation has not been verified. Keep skipping it until that is tested.
    if r.get("top_frame_site_key") and r.get("has_cross_site_ancestor", 0):
        return reject('partition_ancestor')
    if r.get("source_type", 0) not in (0, 1, 2):
        return reject('source_type')
    if r.get("source_scheme", 0) not in (0, 1, 2):
        return reject('source_scheme')
    expiry = chrome_us(r["expires_utc"]) // 1000000
    if r["is_persistent"] and expiry <= (time.time() if now_s is None else now_s):
        return reject('expired')
    partition = r.get("top_frame_site_key", "")
    attrs = {}
    if partition:
        try:
            u = urlsplit(partition)
            port = u.port
        except ValueError:
            return reject('partition_format')
        if u.scheme not in ("http", "https") or not u.hostname or u.path not in ("", "/"):
            return reject('partition_format')
        if port is not None:
            return reject('partition_port')
        attrs["partitionKey"] = f"({u.scheme},{u.hostname})"
    encrypted = bytes(r["encrypted_value"])
    value = decrypt(encrypted) if encrypted else r["value"].encode()
    if value is None:
        return reject('cipher_unsupported')
    if encrypted and version >= 24:
        expected = hashlib.sha256(r["host_key"].encode()).digest()
        if value[:32] != expected:
            raise MigrationError("Cookie host integrity check failed.")
        value = value[32:]
    try:
        value = value.decode("utf-8")
    except UnicodeError:
        return reject('encoding')
    same_site = {-1: 256, 0: 0, 1: 1, 2: 2}.get(r["samesite"])
    if same_site is None:
        return reject('same_site')
    return {"host": r["host_key"], "path": r["path"], "name": r["name"], "value": value,
            "secure": bool(r["is_secure"]), "httpOnly": bool(r["is_httponly"]),
            "session": not bool(r["is_persistent"]), "expiry": max(0, expiry),
            "originAttributes": attrs, "sameSite": same_site, "schemeMap": {0: 0, 1: 1, 2: 2}.get(r.get("source_scheme", 0), 0)}
