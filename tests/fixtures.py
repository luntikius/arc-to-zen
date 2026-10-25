import json
import sqlite3
import struct

from arc_to_zen.common import APPLE_EPOCH, CHROME_EPOCH_US

NOW = 1760000000000000


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def dictionary(values):
    return [v for pair in values.items() for v in pair]


def synthetic_arc(root):
    cache = {"Default": {"name": "Your Arc"}, "Profile 1": {"name": "work"}}
    write_json(root / "User Data/Local State", {"profile": {"info_cache": cache}})
    items, spaces, favorites = {}, {}, []
    def add(id, typ, children=(), title=None, **data):
        items[id] = {"id": id, "data": {typ: data}, "childrenIds": list(children),
                     "title": title, "createdAt": NOW / 1e6 - APPLE_EPOCH}
    for i, key in enumerate(cache):
        p = root / "User Data" / key
        p.mkdir(parents=True, exist_ok=True)
        write_json(p / "Preferences", {"profile": {"name": cache[key]["name"]}})
        with sqlite3.connect(p / "History") as db:
            db.executescript("CREATE TABLE urls(id INTEGER PRIMARY KEY,url TEXT,title TEXT); CREATE TABLE visits(id INTEGER PRIMARY KEY,url INTEGER,visit_time INTEGER,transition INTEGER);")
            db.execute("INSERT INTO urls VALUES (1,?,?)", (f"https://profile{i}.example/history", "Example"))
            db.execute("INSERT INTO visits VALUES (1,1,?,1)", (CHROME_EPOCH_US + NOW,))
        with sqlite3.connect(p / "Login Data") as db:
            db.execute("CREATE TABLE logins(origin_url TEXT,action_url TEXT,username_element TEXT,username_value TEXT,password_element TEXT,password_value BLOB,signon_realm TEXT,scheme INTEGER,date_created INTEGER,times_used INTEGER,blacklisted_by_user INTEGER)")
        with sqlite3.connect(p / "Cookies") as db:
            db.executescript("CREATE TABLE meta(key TEXT,value TEXT); INSERT INTO meta VALUES('version','24'); CREATE TABLE cookies(host_key TEXT,path TEXT,name TEXT,value TEXT,encrypted_value BLOB,expires_utc INTEGER,is_secure INTEGER,is_httponly INTEGER,is_persistent INTEGER,samesite INTEGER,source_scheme INTEGER,top_frame_site_key TEXT,has_cross_site_ancestor INTEGER,source_type INTEGER);")
        with sqlite3.connect(p / "Web Data") as db:
            db.execute("CREATE TABLE autofill(name TEXT,value TEXT,count INTEGER,date_created INTEGER,date_last_used INTEGER)")
        ref = {"default": True} if i == 0 else {"custom": {"_0": {"directoryBasename": key}}}
        sid = "space" + str(i)
        spaces[sid] = {"id": sid, "profile": ref, "title": f"Workspace {i}", "containerIDs": ["pinned", f"pinned{i}", "unpinned", f"loose{i}"],
            "customInfo": {"windowTheme": {"background": {"baseColors": [{"red": 0.2, "green": 0.5, "blue": 0.7}]}}}}
        add(f"pinned{i}", "itemContainer", [f"folder{i}", f"split{i}"] if i == 0 else [f"folder{i}"])
        add(f"loose{i}", "itemContainer", [f"tab{i}b"])
        add(f"favroot{i}", "itemContainer", [f"favorite{i}"])
        favorites.extend([ref, f"favroot{i}"])
        add(f"folder{i}", "list", [f"tab{i}a", f"nested{i}"] + ([f"split{i}"] if i == 1 else []), title="Folder")
        add(f"nested{i}", "list", [], title="Empty nested folder")
        add(f"tab{i}a", "tab", savedURL="https://same.example/pinned", savedTitle="Pinned original")
        add(f"tab{i}b", "tab", savedURL="https://same.example/pinned", savedTitle="Duplicate URL")
        add(f"favorite{i}", "tab", savedURL="https://favorite.example/", savedTitle="Favorite")
        add(f"split{i}", "splitView", [f"left{i}", f"right{i}"], layoutOrientation="horizontal", itemWidthFactors=[f"left{i}", 0.4, f"right{i}", 0.6])
        add(f"left{i}", "tab", savedURL="https://left.example/", savedTitle="Left")
        add(f"right{i}", "tab", savedURL="https://right.example/", savedTitle="Right")
    write_json(root / "StorableSidebar.json", {"version": 1, "sidebar": {"containers": [{"global": {}}, {"spaces": dictionary(spaces), "items": dictionary(items), "topAppsContainerIDs": favorites}]}})
    write_json(root / "StorableWindows.json", {"version": 1, "lastFocusedSpaceID": "space1", "windows": [{"expandedItems": ["folder0", {"expanded": {}}]}]})
    write_json(root / "StorableSessionRestorationData.json", {"version": 1, "mapping": {"keysToValues": [{"itemId": "tab0a", "browser": {"contextID": {"profile": {"default": True}}}}, 42]}})
    write_json(root / "StorableArchiveItems.json", {"version": 1, "items": []})
    s = root / "User Data/Default/Sessions"
    s.mkdir()
    (s / "Session_100").write_bytes(snss([(6, navigation(42, 0, "https://same.example/pinned", "Pinned original")),
                                          (6, navigation(42, 1, "https://same.example/current", "Current")),
                                          (7, struct.pack("<ii", 42, 1))]))
    (s / "Session_99").write_bytes(snss([(6, navigation(999, 0, "https://stale.example/", "Stale"))]))
    return root


def navigation(tab, index, url, title):
    def string(value, utf16=False):
        raw = value.encode("utf-16-le" if utf16 else "utf-8")
        return struct.pack("<i", len(raw) // (2 if utf16 else 1)) + raw + b"\0" * (-len(raw) % 4)
    payload = struct.pack("<ii", tab, index) + string(url) + string(title, True)
    return struct.pack("<I", len(payload)) + payload


def snss(records):
    return b"SNSS\3\0\0\0" + b"".join(struct.pack("<H", len(p) + 1) + bytes([cmd]) + p for cmd, p in records)
