// Executed only in the dedicated staging profile, with Marionette chrome scope.
// Never return exception messages or source data to the console.
const [op, data] = arguments;
const done = arguments[arguments.length - 1];
(async () => {
  const Services = globalThis.Services;
  const { PlacesUtils } = ChromeUtils.importESModule("resource://gre/modules/PlacesUtils.sys.mjs");
  let SessionStore;
  for (const path of ["moz-src:///browser/components/sessionstore/SessionStore.sys.mjs", "resource:///modules/sessionstore/SessionStore.sys.mjs"]) {
    try { SessionStore = ChromeUtils.importESModule(path).SessionStore; break; } catch (_) {}
  }
  if (!SessionStore) throw new Error("unsupported SessionStore");
  const { LoginHelper } = ChromeUtils.importESModule("resource://gre/modules/LoginHelper.sys.mjs");
  await SessionStore.promiseInitialized;
  const win = Services.wm.getMostRecentWindow("navigator:browser");
  if (op === "identity") {
    const startupOffline = Services.io.offline;
    Services.io.offline = true;
    return { path: PathUtils.profileDir, pid: Services.appinfo.processID, startupOffline };
  }
  const importOne = (paths, name) => {
    for (const path of paths) {
      try { const value = ChromeUtils.importESModule(path)[name]; if (value) return value; } catch (_) {}
    }
    throw new Error("unsupported module");
  };
  const identities = () => importOne([
    "moz-src:///toolkit/components/contextualidentity/ContextualIdentityService.sys.mjs",
    "resource://gre/modules/ContextualIdentityService.sys.mjs"], "ContextualIdentityService");
  const cookieScale = () => {
    // A future seconds timestamp is already expired if this API uses ms.
    // Probe both known contracts; read back the exact expiry, never weaken it.
    const host = "arc-to-zen-capability.invalid";
    const expiry = Math.floor(Date.now() / 1000) + 3600;
    let scale = 0;
    for (const candidate of [1, 1000]) {
      Services.cookies.add(host, "/", "probe", "synthetic", true, true, false,
        expiry * candidate, {}, 2, 2, false);
      const c = [...Services.cookies.getCookiesFromHost(host, {})].find(c => c.name === "probe");
      if (c && c.expiry === expiry * candidate) { scale = candidate; break; }
    }
    Services.cookies.remove(host, "probe", "/", {});
    if (!scale) throw new Error("unknown cookie time units");
    return scale;
  };
  if (op === "capabilities") {
    await win.gZenWorkspaces.promiseInitialized;
    if (typeof win.gZenWorkspaces.getWorkspaces !== "function" ||
        typeof identities().create !== "function" || typeof Services.logins.getAllLogins !== "function" ||
        typeof PlacesUtils.history.insertMany !== "function") throw new Error("capability");
    return {cookieScale: cookieScale(), version: Services.appinfo.version};
  }
  if (op === "containers") {
    const service = identities();
    for (const c of service.getPublicIdentities()) service.remove(c.userContextId);
    const mapping = {};
    const colors = ["blue", "orange", "green", "purple", "red", "turquoise"];
    for (const [i, c] of data.entries())
      mapping[c.id] = service.create(c.name, i ? "briefcase" : "fingerprint", colors[i % colors.length]).userContextId;
    await service.saveSoon();
    return mapping;
  }
  if (op === "verifyContainers") {
    const service = identities();
    await win.gZenWorkspaces.promiseInitialized;
    if (!Services.prefs.getBoolPref("zen.workspaces.separate-essentials")) throw new Error("essentials preference");
    const spaces = win.gZenWorkspaces.getWorkspaces();
    for (const c of data.containers)
      if (service.getPublicIdentityFromId(c.id)?.name !== c.name) throw new Error("container name");
    for (const space of data.spaces) {
      if (!spaces.some(s => s.uuid === space.uuid && s.containerTabId === space.containerTabId))
        throw new Error("workspace container");
    }
    const state = JSON.parse(SessionStore.getBrowserState()).windows[0];
    for (const t of state.tabs.filter(t => !t.zenIsEmpty && !t.zenEssential)) {
      const space = spaces.find(s => s.uuid === t.zenWorkspace);
      if (!space || space.containerTabId !== t.userContextId) throw new Error("tab container");
    }
    return true;
  }
  if (op === "verifyUI") {
    const manager = win.gZenWorkspaces;
    await manager.promiseInitialized;
    const first = manager.activeWorkspace;
    const result = [];
    for (const space of data) {
      await manager.changeWorkspaceWithID(space.uuid);
      if (manager.activeWorkspace !== space.uuid || manager.getCurrentSpaceContainerId() !== space.containerTabId)
        throw new Error("space switch");
      const section = win.document.querySelector(`.zen-essentials-container[container="${space.containerTabId}"]`);
      if (!section || section.hidden || section.hasAttribute("hidden")) throw new Error("essential section hidden");
      for (const other of win.document.querySelectorAll(".zen-essentials-container")) {
        if (other !== section && !other.hasAttribute("hidden")) throw new Error("essential isolation");
      }
      for (const tab of section.querySelectorAll("tab"))
        if (+tab.getAttribute("usercontextid") !== space.containerTabId) throw new Error("essential tab container");
      const element = manager.workspaceElement(space.uuid);
      if (!element || !element.isConnected) throw new Error("space absent from sidebar");
      result.push({space: space.name, profile: identities().getPublicIdentityFromId(space.containerTabId).name,
        essentials: section.querySelectorAll("tab:not([zen-empty-tab])").length});
    }
    await manager.changeWorkspaceWithID(first);
    return result;
  }
  const imageLoads = url => new Promise(resolve => {
    const image = new win.Image();
    const timeout = win.setTimeout(() => resolve(false), 3000);
    image.onload = () => { win.clearTimeout(timeout); resolve(image.naturalWidth > 0); };
    image.onerror = () => { win.clearTimeout(timeout); resolve(false); };
    image.src = url;
  });
  if (op === "appearanceCapabilities") {
    await win.gZenWorkspaces.promiseInitialized;
    const picker = win.gZenThemePicker;
    const slider = win.document.getElementById("PanelUI-zen-gradient-generator-opacity");
    if (!picker || !slider || typeof win.gZenFolders.setFolderUserIcon !== "function") throw new Error("appearance capability");
    const maxColors = picker.constructor.MAX_DOTS;
    const minOpacity = Number(slider.min), maxOpacity = Number(slider.max);
    if (!Number.isInteger(maxColors) || maxColors < 1 || !Number.isFinite(minOpacity) || maxOpacity <= minOpacity)
      throw new Error("appearance capability");
    const icons = [];
    for (const url of new Set(data)) {
      if (!/^chrome:\/\/browser\/skin\/zen-icons\/selectable\/[a-z0-9-]+\.svg$/.test(url)) throw new Error("appearance capability");
      if (await imageLoads(url)) icons.push(url);
    }
    return {icons, maxColors, minOpacity, maxOpacity, harmonies: picker.colorHarmonies.map(h => h.type)};
  }
  if (op === "defaultTheme") {
    await win.gZenWorkspaces.promiseInitialized;
    const picker = win.gZenThemePicker;
    if (typeof picker?.constructor.getTheme !== "function") throw new Error("appearance capability");
    const theme = picker.constructor.getTheme();
    if (theme.type !== "gradient" || !Array.isArray(theme.gradientColors) || theme.gradientColors.length ||
        !Number.isFinite(theme.opacity) || !Number.isFinite(theme.texture)) throw new Error("appearance capability");
    return theme;
  }
  if (op === "applyAppearance") {
    const manager = win.gZenWorkspaces;
    await manager.promiseInitialized;
    for (const patch of data.spaces) {
      const space = manager.getWorkspaces().find(s => s.uuid === patch.uuid);
      if (!space) throw new Error("appearance missing object");
      const updated = {...space};
      for (const key of ["icon", "theme"]) if (key in patch) updated[key] = patch[key];
      await manager.saveWorkspace(updated);
      if ("theme" in patch) win.gZenThemePicker.onWorkspaceChange(updated);
    }
    for (const patch of data.folders) {
      const folder = win.document.getElementById(patch.id);
      if (!folder?.icon) throw new Error("appearance missing object");
      win.gZenFolders.setFolderUserIcon(folder, patch.userIcon);
      folder.dispatchEvent(new win.CustomEvent("TabGroupUpdate", {bubbles: true}));
    }
    await new Promise(resolve => win.setTimeout(resolve, 200));
    return true;
  }
  if (op === "verifyAppearance") {
    const manager = win.gZenWorkspaces;
    await manager.promiseInitialized;
    const first = manager.activeWorkspace;
    const loaded = new Set();
    const checkImage = async url => {
      if (!loaded.has(url) && !await imageLoads(url)) throw new Error("appearance image");
      loaded.add(url);
    };
    for (const space of data.spaces) {
      await manager.changeWorkspaceWithID(space.uuid);
      await new Promise(resolve => win.setTimeout(resolve, 80));
      if (space.icon) {
        const buttons = [...win.document.querySelectorAll("toolbarbutton[zen-workspace-id]")];
        const button = buttons.find(b => b.getAttribute("zen-workspace-id") === space.uuid && b.querySelector(".zen-workspace-icon"));
        if (space.icon.endsWith(".svg")) {
          if (button?.querySelector("img.zen-workspace-icon")?.getAttribute("src") !== space.icon) throw new Error("appearance icon");
          await checkImage(space.icon);
        } else if (button?.querySelector("label.zen-workspace-icon")?.textContent !== space.icon) throw new Error("appearance icon");
      }
      if (space.theme) {
        const picker = win.gZenThemePicker;
        if (picker.currentOpacity !== space.theme.opacity || picker.currentTexture !== space.theme.texture) throw new Error("appearance controls");
        const gradient = picker.getGradientForWorkspace(manager.getWorkspaceFromId(space.uuid), {getGradient: true});
        if (!win.CSS.supports("background-image", gradient.gradient) && !win.CSS.supports("background-color", gradient.gradient)) throw new Error("appearance gradient");
        if (gradient.grain !== space.theme.texture) throw new Error("appearance texture");
      }
    }
    for (const folder of data.folders) {
      if (!folder.userIcon) continue;
      const element = win.document.getElementById(folder.id);
      if (element?.icon?.querySelector("svg .icon image")?.getAttribute("href") !== folder.userIcon) throw new Error("appearance folder");
      await checkImage(folder.userIcon);
    }
    await manager.changeWorkspaceWithID(first);
    return true;
  }
  if (op === "export") {
    await Services.logins.initializationPromise;
    const fields = ["origin", "formActionOrigin", "httpRealm", "username", "password", "usernameField", "passwordField",
      "timeCreated", "timeLastUsed", "timePasswordChanged", "timesUsed"];
    const logins = (await Services.logins.getAllLogins()).map(r => {
      const item = Object.fromEntries(fields.map(f => [f, r[f]]));
      item.usernameElement = item.usernameField; item.passwordElement = item.passwordField;
      delete item.usernameField; delete item.passwordField; return item;
    });
    const db = await PlacesUtils.promiseDBConnection();
    const rows = await db.execute("SELECT p.url,p.title,v.visit_date,v.visit_type FROM moz_historyvisits v JOIN moz_places p ON p.id=v.place_id ORDER BY v.id");
    const history = rows.map(r => ({url: r.getResultByName("url"), title: r.getResultByName("title") || "",
      date: r.getResultByName("visit_date"), transition: r.getResultByName("visit_type")}));
    const scale = cookieScale();
    const cookies = [...Services.cookies.cookies].filter(c => c.isSession || c.expiry > Date.now() / 1000 * scale).map(c => ({host: c.host, path: c.path, name: c.name, value: c.value,
      secure: c.isSecure, httpOnly: c.isHttpOnly, session: c.isSession, expiry: c.expiry / scale,
      originAttributes: c.originAttributes, sameSite: c.sameSite, schemeMap: c.schemeMap}));
    const bookmarkRows = await db.execute("SELECT b.id,b.parent,b.type,b.title,p.url FROM moz_bookmarks b LEFT JOIN moz_places p ON p.id=b.fk ORDER BY b.position");
    const nodes = bookmarkRows.map(r => Object.fromEntries(["id", "parent", "type", "title", "url"].map(k => [k, r.getResultByName(k)])));
    const convert = parent => nodes.filter(n => n.parent === parent).flatMap(n => {
      if (n.type === 1 && n.url) return [{title: n.title || "", url: n.url}];
      if (n.type === 2) { const children = convert(n.id); return children.length ? [{title: n.title || "", children}] : []; }
      return [];
    });
    const roots = await db.execute("SELECT id FROM moz_bookmarks WHERE guid='root________'");
    if (roots.length !== 1) throw new Error("bookmark root");
    const bookmarks = convert(roots[0].getResultByName("id"));
    return {logins, history, cookies, bookmarks, state: JSON.parse(SessionStore.getBrowserState())};
  }

  if (op === "profileGroup") {
    const { ProfilesDatastoreService: store } = ChromeUtils.importESModule(
      "moz-src:///toolkit/profile/ProfilesDatastoreService.sys.mjs");
    const db = await store.getConnection();
    const path = await store.getProfilesStorePath();
    if (!PathUtils.isAbsolute(path) || PathUtils.parent(path) !== PathUtils.profileDir)
      throw new Error("non-isolated profile store");
    const columns = (await db.execute("PRAGMA table_info(Profiles)"))
      .map(row => row.getResultByName("name"));
    if (JSON.stringify(columns) !== JSON.stringify(
      ["id", "path", "name", "avatar", "themeId", "themeFg", "themeBg"]))
      throw new Error("unsupported profile group schema");
    if ((await db.execute("SELECT id FROM Profiles")).length)
      throw new Error("profile builder is not empty");
    await db.executeTransaction(async () => {
      for (const p of data) await db.execute(
        "INSERT INTO Profiles (path,name,avatar,themeId,themeFg,themeBg) " +
        "VALUES (:path,:name,:avatar,:themeId,:themeFg,:themeBg)",
        {path: p.path, name: p.name, avatar: "book", themeId: "default-theme@mozilla.org",
          themeFg: "rgb(21,20,26)", themeBg: "rgb(240,240,244)"});
    });
    const rows = await db.execute("SELECT path,name FROM Profiles ORDER BY id");
    return {id: await store.storeID, path,
      profiles: rows.map(row => ({path: row.getResultByName("path"), name: row.getResultByName("name")}))};
  }
  if (op === "history") {
    const grouped = new Map();
    const skipped = [];
    for (let i = 0; i < data.length; i++) {
      const r = data[i];
      // Chromium can retain URLs that Gecko refuses to store in Places (for
      // example overly long URLs). Use the installed engine's own policy.
      let uri;
      try { uri = Services.io.newURI(r.url); } catch (_) { skipped.push(i); continue; }
      if (!PlacesUtils.history.canAddURI(uri)) { skipped.push(i); continue; }
      if (!grouped.has(r.url)) grouped.set(r.url, { url: new URL(r.url), title: r.title, visits: [] });
      grouped.get(r.url).visits.push({ date: new Date(r.date / 1000), transition: r.transition });
    }
    let errors = 0;
    if (grouped.size) await PlacesUtils.history.insertMany([...grouped.values()], null, () => errors++);
    if (errors) throw new Error("history");
    return { inserted: data.length - skipped.length, skipped };
  }
  if (op === "logins") {
    // Native LoginHelper creates the profile's key4.db and encrypted logins.json.
    await Services.logins.initializationPromise;
    const inserted = await LoginHelper.maybeImportLogins(data);
    return inserted.length;
  }
  if (op === "cookies") {
    let count = 0;
    const skipped = [];
    const scale = cookieScale();
    const rejected = [
      "eRejectedEmptyNameAndValue", "eRejectedNameValueOversize", "eRejectedInvalidCharName",
      "eRejectedInvalidCharValue", "eRejectedInvalidPath", "eRejectedInvalidDomain",
      "eRejectedInvalidPrefix", "eRejectedNoneRequiresSecure", "eRejectedPartitionedRequiresSecure",
      "eRejectedHttpOnlyButFromScript", "eRejectedSecureButNonHttps", "eRejectedForNonSameSiteness",
      "eRejectedAttributePathOversize", "eRejectedAttributeDomainOversize", "eRejectedAttributeExpiryOversize",
    ].map(name => Ci.nsICookieValidation?.[name]).filter(Number.isInteger);
    for (const [index, c] of data.entries()) {
      // Validate the host separately: ILLEGAL_VALUE and INVALID_ARG share a
      // code, so catching it around add() would also hide broken API arguments.
      if (c.host) {
        try { Services.eTLD.getBaseDomainFromHost(c.host); }
        catch (e) {
          if (e.result === Cr.NS_ERROR_ILLEGAL_VALUE) { skipped.push(index); continue; }
          if (![Cr.NS_ERROR_HOST_IS_IP_ADDRESS, Cr.NS_ERROR_INSUFFICIENT_DOMAIN_LEVELS].includes(e.result))
            throw new Error("cookie host API");
        }
      }
      // Chromium stores session expiry as zero; Gecko still requires a future
      // expiry argument when adding an isSession cookie.
      // Zen 1.23.4b's Gecko API takes milliseconds (Chromium uses microseconds).
      const expiry = (c.session ? Date.now() / 1000 + 31536000 : c.expiry) * scale;
      let validation;
      try {
        validation = Services.cookies.add(c.host, c.path, c.name, c.value, c.secure, c.httpOnly,
          c.session, expiry, c.originAttributes, c.sameSite, c.schemeMap, !!c.originAttributes.partitionKey);
      } catch (_) { throw new Error("cookie API failure"); }
      // Older Gecko add() returns void; accepted records are still read back
      // and verified before installation. Newer APIs return a validation result.
      if (validation != null && validation.result !== Ci.nsICookieValidation.eOK) {
        if (!rejected.includes(validation.result)) throw new Error("cookie validation API");
        skipped.push(index);
        continue;
      }
      count++;
    }
    return { inserted: count, skipped };
  }
  if (op === "bookmarks") {
    const add = async (node, parentGuid) => {
      if (node.children) {
        const folder = await PlacesUtils.bookmarks.insert({ parentGuid, title: node.title, type: PlacesUtils.bookmarks.TYPE_FOLDER });
        for (const child of node.children) await add(child, folder.guid);
      } else {
        await PlacesUtils.bookmarks.insert({ parentGuid, title: node.title, url: node.url });
      }
    };
    for (const root of data) await add(root, PlacesUtils.bookmarks.unfiledGuid);
    return data.length;
  }
  if (op === "forms") {
    Services.prefs.setBoolPref("browser.formfill.enable", true);
    const { FormHistory } = ChromeUtils.importESModule("resource://gre/modules/FormHistory.sys.mjs");
    await FormHistory.update(data.map(r => ({ op: "add", ...r })));
    return data.length;
  }
  if (op === "addresses" || op === "cards") {
    let store;
    try {
      const { formAutofillStorage } = ChromeUtils.importESModule("resource://autofill/FormAutofillStorage.sys.mjs");
      await formAutofillStorage.initialize();
      store = op === "cards" ? formAutofillStorage.creditCards : formAutofillStorage.addresses;
      if (!store || typeof store.add !== "function") return { unsupported: data.length };
    } catch (_) {
      return { unsupported: data.length };
    }
    for (const r of data) await store.add(r);
    return data.length;
  }
  if (op === "verifyData") {
    await Services.logins.initializationPromise;
    const actual = await Services.logins.getAllLogins();
    for (const expected of data.logins) {
      if (!actual.some(r => r.origin === expected.origin && r.username === expected.username &&
        r.password === expected.password && r.httpRealm === expected.httpRealm && r.formActionOrigin === expected.formActionOrigin)) {
        throw new Error("login");
      }
    }
    const scale = cookieScale();
    for (const c of data.cookies) {
      if (!c.session && c.expiry <= Date.now() / 1000) continue;
      const actualCookies = Services.cookies.getCookiesFromHost(c.host.replace(/^\./, ""), c.originAttributes);
      const r = [...actualCookies].find(r => r.host === c.host && r.path === c.path && r.name === c.name);
      if (!r) throw new Error("cookie missing");
      if (r.value !== c.value) throw new Error("cookie value");
      if (r.isSecure !== c.secure || r.isHttpOnly !== c.httpOnly || r.sameSite !== c.sameSite)
        throw new Error("cookie security");
      if (r.isSession !== c.session) throw new Error("cookie session");
      if ((r.originAttributes.partitionKey || "") !== (c.originAttributes.partitionKey || "") ||
          r.isPartitioned !== !!c.originAttributes.partitionKey ||
          (r.originAttributes.userContextId || 0) !== (c.originAttributes.userContextId || 0))
        throw new Error("cookie isolation");
      if (!c.session && r.expiry !== c.expiry * scale) throw new Error("cookie expiry");
    }
    const db = await PlacesUtils.promiseDBConnection();
    const rows = await db.execute("SELECT count(*) AS n FROM moz_historyvisits");
    return { logins: actual.length, history: rows[0].getResultByName("n") };
  }
  if (op === "sidebar") {
    const state = JSON.parse(SessionStore.getBrowserState());
    const w = state.windows[0] || {};
    Object.assign(w, data);
    delete w.lastSessionState;
    w.selected = Math.max(1, w.tabs.findIndex(t => !t.zenIsEmpty) + 1);
    w._closedTabs = [];
    state.windows = [w];
    state._closedWindows = [];
    state.selectedWindow = 1;
    state.session = { lastUpdate: Date.now(), startTime: Date.now(), recentCrashes: 0 };
    const restored = new Promise(resolve => {
      const observer = () => { Services.obs.removeObserver(observer, "sessionstore-browser-state-restored"); resolve(); };
      Services.obs.addObserver(observer, "sessionstore-browser-state-restored");
    });
    SessionStore.setBrowserState(JSON.stringify(state));
    await restored;
    return true;
  }
  if (op === "state") {
    return JSON.parse(SessionStore.getBrowserState());
  }
  if (op === "shutdown") {
    // Persist session cookies for the next browser startup too.
    const state = JSON.parse(SessionStore.getBrowserState());
    const cookies = [...Services.cookies.cookies].filter(c => c.isSession).map(c => ({
      host: c.host, path: c.path, name: c.name, value: c.value, secure: c.isSecure,
      httponly: c.isHttpOnly, expiry: c.expiry, originAttributes: c.originAttributes,
      sameSite: c.sameSite, schemeMap: c.schemeMap, isPartitioned: c.isPartitioned,
    }));
    return { state, cookies };
  }
  throw new Error("unknown operation");
})().then(value => done({ ok: true, value }), e => done({ ok: false, stage: op, reason: ["appearance capability", "appearance missing object", "appearance image", "appearance icon", "appearance controls", "appearance gradient", "appearance texture", "appearance folder", "login", "cookie missing", "cookie value", "cookie security", "cookie session", "cookie isolation", "cookie expiry", "cookie host API", "cookie API failure", "cookie validation API"].includes(e.message) ? e.message : "unsupported data or API" }));
