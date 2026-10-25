import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fixtures import synthetic_arc
from test_appearance import edit_theme_in_native_picker
from arc_to_zen import diagnostics
from arc_to_zen.selection import Selection, catalog
from arc_to_zen.transfer import collect
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
            extra['_arcAppearance'] = {'windowTheme': {'background': {'future': {}}}}
            p.sidebar['spaces'].append(extra)
            # Also cover absent theme independently of an unsupported theme.
            p.sidebar['spaces'][1]['_arcAppearance'].pop('windowTheme', None)
            profile = base / 'profile'
            with diagnostics.recording(base / 'runs', True) as log:
                stage_profile(app, profile, p)
            self.assertEqual((p.theme_count, p.skipped['themes']), (1, 1))
            self.assertEqual(p.sidebar['spaces'][1]['theme']['gradientColors'], [])
            self.assertEqual(p.sidebar['spaces'][2]['theme']['gradientColors'], [])
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
            for _ in range(2):
                with Gecko(app, profile) as g:
                    g.call('verifyAppearance', p.sidebar)
                    # Tabs are unchanged; the native new-tab checks only added
                    # and closed synthetic blank tabs.
                    check_sidebar(p.sidebar, g.call('state', None)['windows'][0])
            events = [json.loads(line) for line in log.path.read_text().splitlines()]
            self.assertTrue(any(r['event'] == 'theme' and r.get('status') == 'unsupported' for r in events))
            self.assertEqual(len([r for r in events if r['event'] == 'theme' and r.get('status') == 'verified']), 6)
            self.assertEqual(len([r for r in events if r['event'] == 'space' and r.get('status') == 'verified']), 6)
