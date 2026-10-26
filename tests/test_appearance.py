import copy
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arc_to_zen.appearance import (ICON_PREFIX, icon_table, icon, theme, patches, apply_patches)
from arc_to_zen.common import MigrationError
from arc_to_zen.source import build_sidebars, discover, identifier
from arc_to_zen.merge import merge_profiles
from arc_to_zen.zen import Gecko, stage_profile, read_lz4, write_lz4, comparable_sidebar
from arc_to_zen.transaction import Transaction, operation_lock, tree_digest
from arc_to_zen.repair_appearance import repair_appearance, check_native_restore, detach
from fixtures import synthetic_arc, write_json

CAPS = {'icons': [ICON_PREFIX + r['zen'] for r in icon_table().values()], 'maxColors': 3,
        'defaultTheme': {'type': 'gradient', 'gradientColors': [], 'opacity': .5, 'texture': 0},
        'minOpacity': .3, 'maxOpacity': .8, 'harmonies': ['floating', 'singleAnalogous', 'analogous', 'complementary']}


def arc_theme(colors=None, **gradient):
    return {'background': {'single': {'_0': {'isVibrant': True, 'contentOverBackgroundAppearance': 'light',
        'style': {'color': {'_0': {'blendedGradient': {'_0': {'baseColors': colors or [
            {'red': .2, 'green': .5, 'blue': .7, 'alpha': 1, 'colorSpace': 'extendedSRGB'}], **gradient}}}}}}}}}


def add_appearance(root):
    path = root / 'StorableSidebar.json'
    value = json.loads(path.read_text()); c = value['sidebar']['containers'][1]
    for s in c['spaces']:
        if isinstance(s, dict):
            s['customInfo'] = {'iconType': {'icon': 'planet'}, 'windowTheme': arc_theme(
                wheel={'analogous': {}}, modifiers={'intensityFactor': .6, 'overlay': 'grain', 'noiseFactor': .4})}
    for item in c['items']:
        if isinstance(item, dict) and 'list' in item['data']:
            item['data']['list']['customInfo'] = {'iconType': {'icon': 'flash'}}
    write_json(path, value)


def edit_theme_in_native_picker(browser, uuid):
    # Exercise the editor's color/opacity handlers and popup-close save path,
    # rather than writing a workspace theme directly.
    return browser.client.execute_async_script('''
      const [uuid, done] = arguments;
      (async () => {
        const win = Services.wm.getMostRecentWindow("navigator:browser");
        const manager = win.gZenWorkspaces, picker = win.gZenThemePicker;
        await manager.promiseInitialized;
        await manager.changeWorkspaceWithID(uuid);
        picker.handlePanelOpen();
        await new Promise(r => win.setTimeout(r, 300));
        picker.customColorInput.value = "#527cbe";
        await picker.addCustomColor();
        const slider = win.document.getElementById("PanelUI-zen-gradient-generator-opacity");
        slider.value = .65;
        slider.dispatchEvent(new win.Event("input", {bubbles: true}));
        picker.handlePanelClose();
        await new Promise(r => win.setTimeout(r, 300));
        return manager.getWorkspaceFromId(uuid).theme;
      })().then(done, () => done(null));
    ''', script_args=[uuid], new_sandbox=False)


class AppearanceTests(unittest.TestCase):
    def test_every_mapping_is_unique_and_shared_and_available(self):
        table = icon_table()
        self.assertEqual(len(table), 18)
        for name, row in table.items():
            counts = Counter()
            self.assertEqual(icon({'icon': name}, CAPS, counts), ICON_PREFIX + row['zen'])
            self.assertEqual(icon({'icon': name}, CAPS, counts, folder=True), ICON_PREFIX + row['zen'])
            self.assertFalse(counts)
        self.assertEqual(table['flash']['zen'], 'lightning.svg')
        self.assertEqual(table['medical']['match'], 'approximate')

    def test_missing_icon_unknown_emoji_and_duplicate_table(self):
        counts = Counter()
        self.assertIsNone(icon({'icon': 'future'}, CAPS, counts))
        self.assertIsNone(icon({'icon': 'planet'}, {**CAPS, 'icons': []}, counts))
        self.assertEqual(icon({'emoji': '🪐'}, CAPS, counts), '🪐')
        self.assertIsNone(icon({'emoji': '🪐'}, CAPS, counts, folder=True))
        self.assertEqual(counts['icons'], 3)
        row = {'arc': 'same', 'zen': 'planet.svg', 'match': 'exact'}
        with patch('arc_to_zen.appearance.json.loads', return_value=[row, row]), self.assertRaises(MigrationError):
            icon_table()

    def test_theme_overlay_alpha_intensity_noise_and_native_bounds(self):
        counts = Counter()
        value = arc_theme([{'red': 1, 'green': 0, 'blue': 0, 'alpha': .5}],
            overlayColors=[{'red': 0, 'green': 0, 'blue': 1, 'alpha': .5}],
            modifiers={'intensityFactor': 1, 'overlay': 'grain', 'noiseFactor': .4})
        result = theme(value, CAPS, counts)
        self.assertEqual(result['gradientColors'][0]['c'], [85, 0, 170])
        self.assertEqual(result['gradientColors'][0]['lightness'], 33)
        self.assertFalse(result['gradientColors'][0]['isCustom'])
        self.assertEqual(result['opacity'], .75)
        self.assertEqual(result['texture'], .4)
        self.assertEqual(counts, {'themeApproximations': 1})
        self.assertEqual(theme(arc_theme(modifiers={'intensityFactor': 0}), CAPS, counts)['opacity'], .3)
        self.assertEqual(theme(arc_theme(), CAPS, counts)['opacity'], .8)

    def test_gradient_order_harmony_unknown_theme_and_no_theme(self):
        counts = Counter()
        colors = [{'red': 1, 'green': 0, 'blue': 0}, {'red': 0, 'green': 0, 'blue': 1}]
        result = theme(arc_theme(colors, wheel={'analogous': {}}), CAPS, counts)
        self.assertEqual([c['c'] for c in result['gradientColors']], [[255, 0, 0], [0, 0, 255]])
        self.assertEqual(result['gradientColors'][0]['algorithm'], 'singleAnalogous')
        counts.clear()
        self.assertIsNone(theme({}, CAPS, counts)); self.assertFalse(counts)
        self.assertIsNone(theme({'background': {'future': {}}}, CAPS, counts))
        self.assertEqual(counts['themes'], 1)
        self.assertIsNone(theme(arc_theme([{'red': float('nan'), 'green': 0, 'blue': 0}]), CAPS, counts))

    def test_native_refreshed_title_is_allowed_but_navigation_changes_are_not(self):
        state = {'spaces': [], 'folders': [], 'splitViewData': [], 'tabs': [
            {'zenSyncId': 'tab', 'entries': [{'url': 'https://synthetic.example/', 'title': 'Original'}], 'index': 1}]}
        actual = copy.deepcopy(state)
        actual['tabs'][0]['entries'][0]['title'] = 'Refreshed by Gecko'
        check_native_restore(state, actual)
        self.assertEqual(state['tabs'][0]['entries'][0]['title'], 'Original')
        actual['tabs'][0]['entries'][0]['url'] = 'https://changed.example/'
        with self.assertRaises(MigrationError):
            check_native_restore(state, actual)

    def test_patch_matches_ids_preserves_unknowns_and_is_idempotent(self):
        sidebar = {'spaces': [{'uuid': 'one', 'name': 'renamed', 'icon': 'keep'}, {'uuid': 'new', 'icon': 'keep'}],
                   'folders': [{'id': 'folder', 'userIcon': 'keep'}]}
        source = {'spaces': {'one': {'iconType': {'icon': 'planet'}}, 'removed': {'iconType': {'icon': 'code'}}},
                  'folders': {'folder': {'iconType': {'icon': 'future'}}}}
        changes = patches(sidebar, source, CAPS, Counter()); apply_patches(sidebar, changes)
        self.assertEqual(sidebar['spaces'][0]['name'], 'renamed')
        self.assertEqual(sidebar['spaces'][1]['icon'], 'keep')
        self.assertEqual(sidebar['folders'][0]['userIcon'], 'keep')
        self.assertEqual(patches(sidebar, source, CAPS, Counter()), {'spaces': [], 'folders': []})


class PaletteFallbackTests(unittest.TestCase):
    def test_unknown_color_background_uses_only_mid_tone_and_native_defaults(self):
        value = {'background': {'single': {'_0': {'style': {'color': {'_0': {'futureColor': {}}}}}}},
                 'primaryColorPalette': {'midTone': {'red': .2, 'green': .4, 'blue': .6, 'alpha': .1}}}
        skipped = Counter()
        result = theme(value, CAPS, skipped)
        self.assertEqual(result['gradientColors'][0]['c'], [51, 102, 153])
        self.assertEqual((result['opacity'], result['texture']), (.5, 0))
        self.assertEqual(skipped, {'paletteThemes': 1})
        # The same adapter backs appearance-only updates and is idempotent.
        source = {'spaces': {'a': {'windowTheme': value}}, 'folders': {}}
        sidebar = {'spaces': [{'uuid': 'a'}], 'folders': []}
        changes = patches(sidebar, source, CAPS, Counter())
        apply_patches(sidebar, changes)
        self.assertEqual(sidebar['spaces'][0]['theme'], result)
        self.assertFalse(any(patches(sidebar, source, CAPS, Counter()).values()))
        for palette in (None, {}, {'midTone': {'red': float('nan'), 'green': 0, 'blue': 0}},
                        {'midTone': {'red': 1, 'green': 0, 'blue': 0, 'colorSpace': 'future'}}):
            value['primaryColorPalette'] = palette
            skipped = Counter()
            self.assertIsNone(theme(value, CAPS, skipped))
            self.assertEqual(skipped['themes'], 1)
            self.assertEqual(skipped['paletteThemes'], 0)
        self.assertIsNone(theme(None, CAPS, Counter()))


@unittest.skipUnless(os.environ.get('ARC_TO_ZEN_INTEGRATION') == '1', 'Opt in to isolated Zen checks')
class AppearanceIntegrationTests(unittest.TestCase):
    def test_migration_then_appearance_only_repair_and_rollback(self):
        app = Path('/Applications/Zen.app')
        with tempfile.TemporaryDirectory(prefix='arc-appearance-test-') as t:
            base = Path(t); arc = synthetic_arc(base / 'Arc')
            profiles = discover(arc); build_sidebars(arc, profiles)
            p = merge_profiles(list(profiles.values()))
            p.sidebar['spaces'][0]['_arcAppearance']['iconType'] = {'emoji': '🪐'}
            p.sidebar['folders'][1]['_arcAppearance']['iconType'] = {'icon': 'star'}
            p.sidebar['spaces'][1]['_arcAppearance']['windowTheme'] = arc_theme([
                {'red': .2, 'green': .4, 'blue': .7}, {'red': .5, 'green': .2, 'blue': .6}],
                wheel={'analogous': {}}, modifiers={'overlay': 'grain', 'noiseFactor': .25})
            p.logins = [{'origin': 'https://synthetic.example', 'username': 'test', 'password': '秘密',
                         'formActionOrigin': '', 'httpRealm': None, 'usernameElement': '', 'passwordElement': ''}]
            target = base / 'zen'; target.mkdir()
            (target / 'profiles.ini').write_text('[General]\nVersion=2\n')
            (target / 'installs.ini').write_text('[TEST]\nDefault=Profiles/test\n')
            runtime = base / 'runs'
            with operation_lock(runtime):
                tx = Transaction.create(runtime, target)
                m = stage_profile(app, tx.stage / 'Profiles/test', p)
                m.pop('sidebar')
                m['sidebar_hash'] = hashlib.sha256(json.dumps(comparable_sidebar(p.sidebar), sort_keys=True).encode()).hexdigest()
                tx.install({'test': m}, lambda: None)
            add_appearance(arc)
            # Arc has no explicit theme for the first Space. Preserve a user's
            # existing theme, including legacy custom-color encoding, when only
            # its icon is changed. Validation must not repair unrelated fields.
            source_file = arc / 'StorableSidebar.json'
            source = json.loads(source_file.read_text())
            for s in source['sidebar']['containers'][1]['spaces']:
                if isinstance(s, dict) and s['id'] == 'space0':
                    s['customInfo'].pop('windowTheme')
            write_json(source_file, source)
            for name in ('zen-sessions.jsonlz4', 'sessionstore.jsonlz4'):
                path = target / 'Profiles/test' / name
                state = read_lz4(path)
                for sidebar in state.get('windows', [state]):
                    first = next(s for s in sidebar['spaces'] if s['uuid'] == identifier('space', 'space0'))
                    first['theme']['gradientColors'].append({'c': '51,128,178', 'isCustom': True,
                        'isPrimary': False, 'algorithm': 'analogous', 'lightness': '45', 'position': None, 'type': None})
                write_lz4(path, state)
            args = SimpleNamespace(runtime=runtime, run=tx.directory.name, zen_root=target, zen_app=app, arc_root=arc, dry_run=True)
            before = tree_digest(base)
            with patch('arc_to_zen.repair_appearance.Gecko', side_effect=AssertionError('dry run launches no browser')):
                repair_appearance(args, lambda: None)
            self.assertEqual(tree_digest(base), before)
            ini = target / 'profiles.ini'
            ini.write_text(ini.read_text().replace('Name=Arc', 'Name=Renamed browser'))
            current_before = tree_digest(target)
            sessions_before = {n: read_lz4(target / 'Profiles/test' / n) for n in ('zen-sessions.jsonlz4', 'sessionstore.jsonlz4')}
            preserved = {str(f.relative_to(target)): f.read_bytes() for f in target.rglob('*') if f.is_file() and not f.is_symlink()
                         and f.name not in ('zen-sessions.jsonlz4', 'sessionstore.jsonlz4', '.arc-to-zen-run.json')}
            args.dry_run = False
            native_call = Gecko.call
            def no_secrets(browser, op, data):
                self.assertNotIn(op, ('export', 'logins', 'verifyData'))
                return native_call(browser, op, data)
            with patch.object(Gecko, 'call', no_secrets):
                run = repair_appearance(args, lambda: None)
            repaired = Transaction(runtime, run); repaired.verify()
            for name, data in preserved.items():
                self.assertEqual((target / name).read_bytes(), data, name)
            stored = read_lz4(target / 'Profiles/test/zen-sessions.jsonlz4')
            self.assertTrue(all(s['icon'].endswith('/planet.svg') for s in stored['spaces']))
            self.assertEqual(stored['spaces'][0]['theme'], sessions_before['zen-sessions.jsonlz4']['spaces'][0]['theme'])
            self.assertTrue(all(f['userIcon'].endswith('/lightning.svg') for f in stored['folders'] if not f['splitViewGroup']))
            def without_appearance(value):
                if isinstance(value, dict):
                    return {k: without_appearance(v) for k, v in value.items() if k not in ('theme', 'icon', 'userIcon')}
                if isinstance(value, list):
                    return [without_appearance(v) for v in value]
                return value
            for name, state in sessions_before.items():
                self.assertEqual(without_appearance(read_lz4(target / 'Profiles/test' / name)), without_appearance(state))
            unchanged = tree_digest(target)
            self.assertEqual(repair_appearance(args, lambda: None), run)
            self.assertEqual(tree_digest(target), unchanged)
            repaired.rollback(False, lambda: None)
            self.assertEqual(tree_digest(target), current_before)
            def failed_verify(browser, op, data):
                if op == 'verifyAppearance':
                    raise MigrationError('synthetic failed appearance validation')
                return native_call(browser, op, data)
            with patch.object(Gecko, 'call', failed_verify), self.assertRaises(MigrationError):
                repair_appearance(args, lambda: None)
            self.assertEqual(tree_digest(target), current_before)
            # Explicit reset replaces the malformed theme only, ignoring Arc.
            args.reset_theme = identifier('space', 'space0')
            args.dry_run = True
            with patch('arc_to_zen.repair_appearance.Gecko', side_effect=AssertionError('dry run launches no browser')), \
                 patch('arc_to_zen.repair_appearance.source_appearance', side_effect=AssertionError('reset reads no Arc')):
                repair_appearance(args, lambda: None)
            self.assertEqual(tree_digest(target), current_before)
            args.dry_run = False
            with patch('arc_to_zen.repair_appearance.source_appearance', side_effect=AssertionError('reset reads no Arc')):
                reset_run = repair_appearance(args, lambda: None)
            reset_tx = Transaction(runtime, reset_run); reset_tx.verify()
            reset_state = read_lz4(target / 'Profiles/test/zen-sessions.jsonlz4')
            default = {'type': 'gradient', 'gradientColors': [], 'opacity': .5, 'texture': 0}
            self.assertEqual(reset_state['spaces'][0]['theme'], default)
            expected = copy.deepcopy(sessions_before['zen-sessions.jsonlz4'])
            expected['spaces'][0]['theme'] = default
            self.assertEqual(reset_state, expected)
            for name, data in preserved.items():
                self.assertEqual((target / name).read_bytes(), data, name)
            self.assertEqual(repair_appearance(args, lambda: None), reset_run)
            args.reset_theme = 'unknown'
            with self.assertRaises(MigrationError):
                repair_appearance(args, lambda: None)
            args.reset_theme = identifier('space', 'space0')
            editor = base / 'editor'
            shutil.copytree(target / 'Profiles/test', editor); detach(editor)
            with Gecko(app, editor) as g:
                changed = edit_theme_in_native_picker(g, args.reset_theme)
                self.assertIsNotNone(changed)
                self.assertEqual(changed['opacity'], .65)
                self.assertEqual(changed['gradientColors'][0]['c'], '#527cbe')
            for _ in range(2):
                with Gecko(app, editor) as g:
                    g.call('verifyAppearance', {'spaces': [{'uuid': args.reset_theme, 'theme': changed}], 'folders': []})
                    actual = next(s for s in g.call('state', None)['windows'][0]['spaces'] if s['uuid'] == args.reset_theme)
                    self.assertEqual(actual['theme'], changed)
            reset_tx.rollback(False, lambda: None)
            self.assertEqual(tree_digest(target), current_before)
