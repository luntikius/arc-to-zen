"""One physical profile; Arc identities become Zen contextual identities."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .common import MigrationError
from .source import Profile


def merge_profiles(profiles):
    result = Profile("combined", "Arc", Path("."))
    result.containers = []
    result.sidebar = {k: [] for k in ("tabs", "spaces", "folders", "groups", "splitViewData")}
    visits, logins, forms = set(), {}, {}
    identities = {k: set() for k in ("tabs", "spaces", "folders")}
    for context, original in enumerate(profiles, 1):
        p = deepcopy(original)
        result.containers.append({"id": context, "name": p.name})
        # Repair accepts the old one-physical-profile-per-Arc-profile layout.
        # Existing non-default containers need an explicit mapping, never flatten.
        if any(t.get("userContextId", 0) for t in p.sidebar["tabs"] if not t.get("zenIsEmpty")):
            raise MigrationError("Source already contains contextual tabs; refusing to flatten their isolation.")
        for s in p.sidebar["spaces"]:
            if s.get("containerTabId", 0):
                raise MigrationError("Source already contains contextual Spaces; refusing to flatten them.")
            s["containerTabId"] = context
        for t in p.sidebar["tabs"]:
            t["userContextId"] = context
            # Old principals contain the old context. Gecko recreates them from
            # the restored URL under the new tab's userContextId.
            def strip(value):
                if isinstance(value, dict):
                    for key in list(value):
                        if key in ("triggeringPrincipal_base64", "principalToInherit_base64", "storagePrincipalToInherit_base64"):
                            del value[key]
                        else:
                            strip(value[key])
                elif isinstance(value, list):
                    for item in value:
                        strip(item)
            strip(t)
        for kind, key in (("tabs", "zenSyncId"), ("spaces", "uuid"), ("folders", "id")):
            for obj in p.sidebar[kind]:
                if kind == "tabs" and obj.get("zenIsEmpty"):
                    continue
                identity = obj.get(key)
                if not identity or identity in identities[kind]:
                    raise MigrationError("Missing or conflicting Zen structural identity; no data replaced.")
                identities[kind].add(identity)
        for kind in result.sidebar:
            result.sidebar[kind].extend(p.sidebar.get(kind, []))
        for c in p.cookies:
            if c["originAttributes"].get("userContextId", 0):
                raise MigrationError("Source contains contextual cookies; refusing to flatten their isolation.")
            c["originAttributes"]["userContextId"] = context
            result.cookies.append(c)
        for r in p.history:
            identity = (r["url"], r["date"], r["transition"])
            if identity not in visits:
                visits.add(identity)
                result.history.append(r)
        for r in p.logins:
            key = (r["origin"], r.get("formActionOrigin"), r.get("httpRealm"), r["username"])
            if key in logins and logins[key]["password"] != r["password"]:
                raise MigrationError("Conflicting passwords for the same login. Originals retained; resolve the conflict in Zen first.")
            if key not in logins:
                logins[key] = r
            else:
                for field in ("timeLastUsed", "timePasswordChanged", "timesUsed"):
                    logins[key][field] = max(logins[key].get(field, 0), r.get(field, 0))
        for r in p.forms:
            key = (r["fieldname"], r["value"])
            if key not in forms:
                forms[key] = r
            else:
                forms[key]["timesUsed"] += r["timesUsed"]
                forms[key]["firstUsed"] = min(forms[key]["firstUsed"], r["firstUsed"])
                forms[key]["lastUsed"] = max(forms[key]["lastUsed"], r["lastUsed"])
        for kind in ("bookmarks", "addresses", "cards"):
            getattr(result, kind).extend(getattr(p, kind))
        result.skipped.update(p.skipped)
        result.archived_visits += p.archived_visits
    # SessionStore serializes all Essentials, then all pins, then ordinary tabs.
    result.sidebar["tabs"].sort(key=lambda t: 0 if t.get("zenEssential") else 1 if t.get("pinned") else 2)
    result.logins = list(logins.values())
    result.forms = list(forms.values())
    if not result.sidebar["spaces"]:
        raise MigrationError("No Spaces to migrate.")
    result.sidebar["activeZenSpace"] = result.sidebar["spaces"][0]["uuid"]
    return result


def remap_contexts(profile, mapping):
    for space in profile.sidebar["spaces"]:
        space["containerTabId"] = mapping[space["containerTabId"]]
    for tab in profile.sidebar["tabs"]:
        tab["userContextId"] = mapping[tab["userContextId"]]
    for cookie in profile.cookies:
        attrs = cookie["originAttributes"]
        attrs["userContextId"] = mapping[attrs["userContextId"]]
    for container in profile.containers:
        container["id"] = mapping[container["id"]]
