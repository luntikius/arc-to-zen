"""Arc appearance adapters. The icon table is shared by Spaces and folders."""
from __future__ import annotations

import colorsys
import hashlib
import json
import math
from pathlib import Path

from .common import MigrationError, pairs
from . import diagnostics

ICON_PREFIX = 'chrome://browser/skin/zen-icons/selectable/'


def icon_table():
    rows = json.loads(Path(__file__).with_name('icon_mapping.json').read_text())
    result = {}
    for row in rows:
        name, svg = row['arc'], row['zen']
        if name in result or Path(svg).name != svg or not svg.endswith('.svg') or row['match'] not in ('exact', 'approximate'):
            raise MigrationError('Invalid bundled icon mapping table.')
        result[name] = row
    return result


def capabilities(browser):
    result = browser.call('appearanceCapabilities', [ICON_PREFIX + r['zen'] for r in icon_table().values()])
    result['defaultTheme'] = browser.call('defaultTheme', None)
    return result


def icon(value, caps, skipped, folder=False):
    if not value:
        return None
    if isinstance(value, dict) and isinstance(value.get('emoji'), str):
        if not folder and value['emoji']:
            return value['emoji']
    elif isinstance(value, dict) and isinstance(value.get('icon'), str) and value['icon'] in icon_table():
        url = ICON_PREFIX + icon_table()[value['icon']]['zen']
        if url in caps['icons']:
            return url
    skipped['icons'] += 1
    return None


def unit(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('nonfinite')
    return min(1., max(0., number))


def color(value):
    rgb = [float(value[k]) for k in ('red', 'green', 'blue')]
    if not all(math.isfinite(x) for x in rgb):
        raise ValueError('nonfinite')
    space = value.get('colorSpace', 'sRGB')
    if space in ('displayP3', 'displayP3ColorSpace'):
        linear = [x / 12.92 if x <= .04045 else ((x + .055) / 1.055) ** 2.4 for x in rgb]
        matrix = ((1.224745, -.224904, 0), (-.042058, 1.042081, 0), (-.019642, -.078655, 1.098537))
        rgb = [sum(a * b for a, b in zip(row, linear)) for row in matrix]
        rgb = [12.92 * x if x <= .0031308 else 1.055 * x ** (1 / 2.4) - .055 for x in rgb]
    elif space not in ('sRGB', 'extendedSRGB'):
        raise ValueError('unknown color space')
    return [unit(x) for x in rgb] + [unit(value.get('alpha', 1))]


def theme(value, caps, skipped, space=0):
    diagnostics.emit('theme', space=space, stage='source', theme_present=bool(value), input_theme=value,
                     status='started' if value else 'absent')
    if not value:
        return None
    try:
        bg = value['background']
        # Explicitly supported Arc enum wrappers; do not recursively collect
        # colors from unrelated light/dark palettes or future theme schemas.
        if 'baseColors' in bg:
            gradient, settings = bg, {}
        else:
            settings = bg['single']['_0']
            style = settings['style']['color']['_0']
            if isinstance(style, dict) and len(style) == 1 and 'blendedGradient' not in style:
                # Unknown color background: only the explicitly approved Arc
                # palette fallback is supported, never arbitrary nested colors.
                rgba = color(value['primaryColorPalette']['midTone'])
                from copy import deepcopy
                result = deepcopy(caps['defaultTheme'])
                lightness = colorsys.rgb_to_hls(*rgba[:3])[1]
                result['gradientColors'] = [{'c': [round(x * 255) for x in rgba[:3]],
                    'isCustom': False, 'algorithm': 'floating', 'isPrimary': True,
                    'lightness': round(lightness * 100), 'type': 'explicit-lightness'}]
                skipped['paletteThemes'] += 1
                diagnostics.emit('theme', space=space, stage='mapping', status='approximated',
                                 reason='palette', output_theme=result)
                return result
            gradient = style['blendedGradient']['_0']
        bases = [color(c) for c in gradient['baseColors']]
        if not bases:
            raise ValueError('empty palette')
        overlays = [color(c) for c in gradient.get('overlayColors', [])]
        if overlays and len(overlays) != len(bases):
            raise ValueError('unknown overlay layout')
        # Arc's material/translucency and foreground modes have no per-Space
        # Zen equivalent. Recognize their schema but let native Zen derive
        # contrast and vibrancy; record the approximation instead of inventing CSS.
        mode = settings.get('contentOverBackgroundAppearance')
        translucency = gradient.get('translucencyStyle')
        if mode not in (None, 'light', 'dark', 'automatic', 'system') or translucency not in (None, 'light', 'dark', 'automatic', 'system'):
            raise ValueError('unknown appearance mode')
        if settings.get('isVibrant') not in (None, True, False):
            raise ValueError('unknown material')
        if mode is not None or translucency is not None or 'isVibrant' in settings:
            skipped['themeApproximations'] += 1
        modifiers = gradient.get('modifiers', {})
        intensity = float(modifiers.get('intensityFactor', 1))
        if not math.isfinite(intensity) or intensity < 0:
            raise ValueError('intensity')
        if modifiers.get('overlay') not in (None, 'grain', 'none'):
            raise ValueError('unknown texture')
        texture = unit(modifiers.get('noiseFactor', 0)) if modifiers.get('overlay') == 'grain' else 0
        for i, base in enumerate(bases):
            if overlays:
                over = overlays[i]
                alpha = over[3] + base[3] * (1 - over[3])
                bases[i] = [(over[j] * over[3] + base[j] * base[3] * (1 - over[3])) / alpha if alpha else 0 for j in range(3)] + [alpha]
        # Intensity has no identical native control: approximate with saturation
        # and global opacity, leaving Zen to derive contrasting foregrounds.
        opacity = sum(c[3] for c in bases) / len(bases) * min(1, intensity)
        opacity = min(caps['maxOpacity'], max(caps['minOpacity'], opacity))
        wheel = gradient.get('wheel', {})
        if wheel and (not isinstance(wheel, dict) or len(wheel) != 1):
            raise ValueError('unknown harmony')
        harmony = next(iter(wheel), 'floating')
        if harmony == 'analogous' and len(bases) == 2:
            harmony = 'singleAnalogous'
        if harmony not in caps['harmonies']:
            harmony = 'floating'; skipped['themeApproximations'] += 1
        if len(bases) > caps['maxColors']:
            count = caps['maxColors']
            bases = [bases[round(i * (len(bases) - 1) / (count - 1))] for i in range(count)] if count > 1 else bases[:1]
            skipped['themeApproximations'] += 1
        dots = []
        for i, c in enumerate(bases):
            h, lightness, saturation = colorsys.rgb_to_hls(*c[:3])
            rgb = colorsys.hls_to_rgb(h, lightness, min(1, saturation * intensity))
            dots.append({'c': [round(x * 255) for x in rgb], 'isCustom': False, 'algorithm': harmony,
                         'isPrimary': i == 0, 'lightness': round(lightness * 100), 'type': 'explicit-lightness'})
        result = {'type': 'gradient', 'gradientColors': dots, 'opacity': opacity, 'texture': texture}
        diagnostics.emit('theme', space=space, stage='mapping', status='converted', output_theme=result)
        return result
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
        skipped['themes'] += 1
        tb = exc.__traceback__
        while tb.tb_next: tb = tb.tb_next
        diagnostics.emit('theme', space=space, stage='mapping', status='unsupported',
                         reason='unknown_schema' if isinstance(exc, (KeyError, TypeError)) else 'conversion',
                         exception=type(exc).__name__, file='appearance.py', line=tb.tb_lineno,
                         field=exc.args[0] if isinstance(exc, KeyError) and exc.args else None)
        return None


def fields(info, caps, skipped, folder=False, space=0):
    if not isinstance(info, dict):
        skipped['appearance'] += 1
        return {}
    result = {}
    mapped = icon(info.get('iconType'), caps, skipped, folder)
    if mapped is not None:
        result['userIcon' if folder else 'icon'] = mapped
    if not folder:
        mapped = theme(info.get('windowTheme'), caps, skipped, space)
        if mapped is not None:
            result['theme'] = mapped
    return result


def resolve_sidebar(sidebar, caps, skipped):
    for kind in ('spaces', 'folders'):
        for item in sidebar[kind]:
            if '_arcAppearance' in item:
                item.update(fields(item.pop('_arcAppearance'), caps, skipped, kind == 'folders',
                    diagnostics.alias('space', item['uuid']) if kind == 'spaces' else 0))


def source_appearance(root):
    from .source import arc_container, identifier
    c = arc_container(root)
    spaces = {identifier('space', s['id']): s.get('customInfo') or {} for _, s in pairs(c['spaces'])}
    folders = {identifier('item', i['id']): i['data']['list'].get('customInfo') or {}
               for _, i in pairs(c['items']) if 'list' in i.get('data', {})}
    return {'spaces': spaces, 'folders': folders}


def patches(sidebar, source, caps, skipped):
    result = {'spaces': [], 'folders': []}
    for kind, key in (('spaces', 'uuid'), ('folders', 'id')):
        for obj in sidebar[kind]:
            if obj[key] not in source[kind]:
                continue
            mapped = fields(source[kind][obj[key]], caps, skipped, kind == 'folders',
                diagnostics.alias('space', obj[key]) if kind == 'spaces' else 0)
            changed = {k: v for k, v in mapped.items() if obj.get(k) != v}
            if changed:
                result[kind].append({key: obj[key], **changed})
    return result


def apply_patches(sidebar, changes):
    for kind, key in (('spaces', 'uuid'), ('folders', 'id')):
        indexed = {p[key]: p for p in changes[kind]}
        for obj in sidebar.get(kind, []):
            if obj[key] in indexed:
                obj.update(indexed[obj[key]])


def appearance_digest(sidebar):
    values = {'spaces': [{k: s[k] for k in ('uuid', 'icon', 'theme') if k in s} for s in sidebar['spaces']],
              'folders': [{k: f[k] for k in ('id', 'userIcon') if k in f} for f in sidebar['folders']]}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
