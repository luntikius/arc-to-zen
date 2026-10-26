import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fixtures import synthetic_arc, write_json
from test_appearance import edit_theme_in_native_picker
from arc_to_zen import diagnostics
from arc_to_zen.selection import Selection, catalog
from arc_to_zen.transfer import collect, counts
from arc_to_zen.zen import Gecko, stage_profile, check_sidebar


@unittest.skipUnless(os.environ.get('ARC_TO_ZEN_INTEGRATION') == '1', 'Opt in to isolated Zen checks')
class NativeSpaceDiagnosticsTests(unittest.TestCase):
    def test_three_spaces_default_indicators_new_tabs_and_editable_themes(self):
        app = Path('/Applications/Zen.app')
        with tempfile.TemporaryDirectory(prefix='atz-space-diagnostics-') as t:
            base = Path(t); arc = synthetic_arc(base / 'Arc')
            selection = Selection.from_args(SimpleNamespace(profiles=None, spaces=None, categories=['tabs', 'themes']), catalog(arc))
            p = collect(arc, selection)
            extra = copy.deepcopy(p.sidebar['spaces'][0]); extra['uuid'] = 'synthetic-third-space'
            extra['_arcAppearance'] = {'windowTheme': {'background': {'single': {'_0': {'style': {'color': {'_0': {'future': {}}}}}}},
                'primaryColorPalette': {'midTone': {'red': .4, 'green': .6, 'blue': .8}}}}
            p.sidebar['spaces'].append(extra)
            # Also cover absent theme independently of an unsupported theme.
            p.sidebar['spaces'][1]['_arcAppearance'].pop('windowTheme', None)
            profile = base / 'profile'
            with diagnostics.recording(base / 'runs', True) as log:
                stage_profile(app, profile, p)
            self.assertEqual((p.theme_count, p.skipped['themes']), (2, 0))
            self.assertEqual(p.sidebar['spaces'][1]['theme']['gradientColors'], [])
            self.assertEqual(p.sidebar['spaces'][2]['theme']['gradientColors'][0]['c'], [102, 153, 204])
            self.assertEqual(p.skipped['paletteThemes'], 1)
            with Gecko(app, profile) as g:
                for space in p.sidebar['spaces']:
                    result = g.client.execute_async_script('''
                      const [uuid, otherContext, done] = arguments;
                      (async () => {
                        const win = Services.wm.getMostRecentWindow("navigator:browser");
                        await win.gZenWorkspaces.changeWorkspaceWithID(uuid);
                        // Zen's empty-tab command can only focus its URL bar;
                        // create a real navigation tab through the native API.
                        const tab = win.gBrowser.addTrustedTab("https://new-tab.example/", {inBackground:true, skipAnimation:true});
                        const result = {context: +tab.getAttribute("usercontextid"),
                          isDefault: tab.getAttribute("zenDefaultUserContextId")};
                        win.gBrowser.removeTab(tab, {animate: false});
                        const foreign = win.gBrowser.addTrustedTab("about:blank", {userContextId:otherContext, inBackground:true});
                        result.foreignDefault = foreign.getAttribute("zenDefaultUserContextId");
                        result.foreignVisible = win.getComputedStyle(foreign.querySelector(".tab-context-line")).display !== "none";
                        win.gBrowser.removeTab(foreign, {animate:false});
                        return result;
                      })().then(done, () => done(null));
                    ''', script_args=[space['uuid'], next(c['id'] for c in p.containers if c['id'] != space['containerTabId'])], new_sandbox=False)
                    self.assertEqual(result['context'], space['containerTabId'])
                    self.assertEqual(result['isDefault'], 'true')
                    self.assertNotEqual(result['foreignDefault'], 'true')
                    self.assertTrue(result['foreignVisible'])
                # Exercise native editor save, then verify the changed color on
                # two subsequent startups for imported and fallback themes.
                for space in p.sidebar['spaces']:
                    changed = edit_theme_in_native_picker(g, space['uuid'])
                    self.assertIsNotNone(changed)
                    space['theme'] = changed
                g.call('verifyAppearance', p.sidebar)
                g.client.execute_script('Services.prefs.setBoolPref("zen.workspaces.hide-default-container-indicator", false)')
                observed = g.call('verifyUI', p.sidebar['spaces'])
                self.assertTrue(all(not row['hideDefaultIndicator'] for row in observed))
                self.assertTrue(all(not row['essentialsIndicatorsHidden'] for row in observed))
            for _ in range(2):
                with Gecko(app, profile) as g:
                    g.call('verifyAppearance', p.sidebar)
                    # Tabs are unchanged; the native new-tab checks only added
                    # and closed synthetic blank tabs.
                    check_sidebar(p.sidebar, g.call('state', None)['windows'][0])
            events = [json.loads(line) for line in log.path.read_text().splitlines()]
            self.assertTrue(any(r['event'] == 'theme' and r.get('status') == 'approximated' for r in events))
            self.assertEqual(len([r for r in events if r['event'] == 'theme' and r.get('status') == 'verified']), 6)
            self.assertEqual(len([r for r in events if r['event'] == 'space' and r.get('status') == 'verified']), 6)

    def test_container_without_space_keeps_essentials_and_data(self):
        with tempfile.TemporaryDirectory(prefix='atz-container-only-') as t:
            base = Path(t); arc = synthetic_arc(base / 'Arc')
            shutil.copytree(arc / 'User Data/Profile 1', arc / 'User Data/Profile 2')
            state = json.loads((arc / 'User Data/Local State').read_text())
            state['profile']['info_cache']['Profile 2'] = {'name': 'Unassigned'}
            write_json(arc / 'User Data/Local State', state)
            sidebar = json.loads((arc / 'StorableSidebar.json').read_text())
            c = sidebar['sidebar']['containers'][1]
            c['topAppsContainerIDs'] += [{'custom': {'_0': {'directoryBasename': 'Profile 2'}}}, 'orphan-root']
            c['items'] += ['orphan-root', {'id': 'orphan-root', 'data': {'itemContainer': {}}, 'childrenIds': ['orphan-favorite']},
                          'orphan-favorite', {'id': 'orphan-favorite', 'data': {'tab': {'savedURL': 'https://orphan.example/'}}, 'childrenIds': []}]
            write_json(arc / 'StorableSidebar.json', sidebar)
            data = catalog(arc)
            for mode in ('all', 'none', 'data'):
                with self.subTest(mode=mode):
                    selection = Selection.from_args(SimpleNamespace(profiles=None, spaces=['none'] if mode == 'none' else None,
                        categories=['history'] if mode == 'data' else ['tabs', 'history']), data)
                    p = collect(arc, selection)
                    self.assertEqual(len(p.containers), 3)
                    self.assertEqual(len(p.sidebar['spaces']), 2 if mode == 'all' else 0)
                    self.assertEqual(sum(bool(t.get('zenEssential')) for t in p.sidebar['tabs']), 0 if mode == 'data' else 3)
                    p.cookies = [{'host': 'orphan.example', 'path': '/', 'name': 'test', 'value': 'synthetic',
                        'secure': True, 'httpOnly': True, 'session': True, 'expiry': 0,
                        'originAttributes': {'userContextId': 3}, 'sameSite': 2, 'schemeMap': 2}]
                    stage_profile(Path('/Applications/Zen.app'), base / mode, p)
                    self.assertEqual(counts(p)['spaces'], 2 if mode == 'all' else 0)
                    self.assertEqual(len(p.sidebar['spaces']), 2 if mode == 'all' else 1)
                    self.assertFalse(any(s['containerTabId'] == p.containers[2]['id'] for s in p.sidebar['spaces']))
                    with Gecko(Path('/Applications/Zen.app'), base / mode) as g:
                        # Essentials without a Space are retained in their own
                        # hidden container section, ready for a future Space.
                        result = g.client.execute_script("""
                          const win = Services.wm.getMostRecentWindow('navigator:browser');
                          const section = win.gZenWorkspaces.getEssentialsSection(arguments[0]);
                          return {hidden: section.hasAttribute('hidden'),
                            count: section.querySelectorAll('tab:not([zen-empty-tab])').length};
                        """, script_args=[p.containers[2]['id']])
                        self.assertTrue(result['hidden'])
                        self.assertEqual(result['count'], 0 if mode == 'data' else 1)
