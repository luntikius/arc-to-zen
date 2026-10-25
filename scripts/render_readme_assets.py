"""Render README artwork from the production terminal title and transfer scene.

Uses only synthetic frames, the standard library and the existing renderer.
No browser discovery, UI copy changes or third-party graphics are involved.
"""
from html import escape
import os
from pathlib import Path
import re
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from arc_to_zen.terminal import Terminal, hero  # noqa: E402

ASSETS = ROOT / 'docs/assets'
SGR = re.compile(r'\x1b\[([0-9;]*)m')
PALETTE = {98: '#875fd7', 141: '#af87ff', 209: '#ff875f', 217: '#ffafaf', 245: '#8a8a8a'}


def glyphs(line):
    color, bold, x, offset = '#ece9f2', False, 0, 0
    for match in SGR.finditer(line):
        for char in line[offset:match.start()]:
            yield x, char, color, bold
            x += 1
        codes = [int(v) for v in match[1].split(';') if v] or [0]
        i = 0
        while i < len(codes):
            if codes[i] == 0:
                color, bold = '#ece9f2', False
            elif codes[i] == 1:
                bold = True
            elif codes[i:i+2] == [38, 5]:
                color = PALETTE[codes[i+2]]; i += 2
            elif codes[i:i+2] == [38, 2]:
                color = '#%02x%02x%02x' % tuple(codes[i+2:i+5]); i += 4
            i += 1
        offset = match.end()
    for char in line[offset:]:
        yield x, char, color, bold
        x += 1


def svg(name, lines, width, height, font_size, cell_width, line_height, left, top, title, description):
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
             f'<title id="title">{escape(title)}</title>', f'<desc id="desc">{escape(description)}</desc>',
             f'<rect width="{width}" height="{height}" rx="16" fill="#111015"/>',
             f'<g font-family="Menlo,DejaVu Sans Mono,monospace" font-size="{font_size}" xml:space="preserve">']
    for row, line in enumerate(lines):
        for col, char, color, bold in glyphs(line):
            if char == ' ': continue
            parts.append(f'<text x="{left+col*cell_width:g}" y="{top+row*line_height:g}" fill="{color}" font-weight="{700 if bold else 400}">{escape(char)}</text>')
    parts += ['</g>', '</svg>']
    (ASSETS / name).write_text('\n'.join(parts) + '\n')


def main():
    ASSETS.mkdir(parents=True, exist_ok=True)
    ui = Terminal(); ui.rich = True; ui.colors = True; ui.fd = 0
    frames = []
    ui.write = frames.append
    ui.key = lambda timeout: 'enter'
    with patch('arc_to_zen.terminal.time.monotonic', side_effect=[0, 1.2, 1.2]), \
         patch('arc_to_zen.terminal.os.get_terminal_size', return_value=os.terminal_size((64, 18))):
        ui.intro()
    # Crop the actual fully revealed startup title; omit interactive hints.
    raw = frames[0].replace('\x1b[H', '').splitlines()
    line = next(line.removesuffix('\x1b[K') for line in raw if 'ArcToZen' in line)
    title_glyphs = list(glyphs(line))
    start = next(col for col, char, *_ in title_glyphs if char != ' ')
    end = max(col for col, char, *_ in title_glyphs if char != ' ')
    title_width = (end - start + 1) * 48
    svg('wordmark.svg', [line], 1080, 200, 80, 48, 96,
        (1080-title_width)/2-start*48, 127, 'ArcToZen', 'The violet ArcToZen wordmark with a terminal prompt on the left, from the startup animation.')
    svg('migration.svg', hero('transfer', .45, 96, colors=True), 1080, 420, 18, 10.8, 20,
        (1080-96*10.8)/2, 42, 'From Arc to Zen',
        'A frame from the terminal migration animation: hollow colored Arc and Zen contours, with data packets traveling from left to right.')
    print('Rendered wordmark.svg and migration.svg from the terminal renderer.')


if __name__ == '__main__':
    main()
