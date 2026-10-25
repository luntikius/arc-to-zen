"""Capture the real synthetic wizard through a PTY, then render its terminal cells.

Developer-only dependencies: Pillow and pyte. Requires macOS Menlo.
Only --demo --dry-run is launched; no real browser data or apps are accessed.
"""
import codecs
import fcntl
import os
from pathlib import Path
import pty
import select
import signal
import struct
import sys
import termios
import time

import pyte
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/assets'
COLS, ROWS = 104, 42
CELL, LINE, PAD = 10, 20, 16
BG, FG = '#111015', '#ece9f2'
FONT = '/System/Library/Fonts/Menlo.ttc'


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.Stream(screen)
    decoder = codecs.getincrementaldecoder('utf-8')()
    fonts = [ImageFont.truetype(FONT, 16, index=i) for i in (0, 1)]
    frames, durations = [], []
    samples = []
    best = {}
    last_sample = [0.]
    pending = ['']
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.update(TERM='xterm-256color', PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE='1')
        os.environ.pop('NO_COLOR', None)
        os.execv(sys.executable, [sys.executable, '-m', 'arc_to_zen.cli', 'wizard', '--demo', '--dry-run'])
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', ROWS, COLS, 0, 0))

    def color(value, default):
        named = {'black': '#000000', 'white': '#ffffff', 'red': '#cd0000', 'green': '#00cd00',
                 'blue': '#0000ee', 'magenta': '#cd00cd', 'cyan': '#00cdcd', 'brown': '#cdcd00'}
        return default if value == 'default' else named.get(value, '#' + value)

    def render():
        image = Image.new('RGB', (COLS*CELL + 2*PAD, ROWS*LINE + 2*PAD), BG)
        draw = ImageDraw.Draw(image)
        for y, row in screen.buffer.items():
            for x, cell in row.items():
                if not cell.data.strip(): continue
                ink = color(cell.fg, FG)
                draw.text((PAD + x*CELL, PAD + y*LINE + 16), cell.data,
                          font=fonts[bool(cell.bold)], fill=ink, anchor='ls')
        return image

    def pump(duration):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if select.select([fd], [], [], .025)[0]:
                try: chunk = os.read(fd, 65536)
                except OSError: return
                if not chunk: return
                pending[0] += decoder.decode(chunk)
                # A PTY may split a single redraw across reads. Publish frames
                # only after the renderer's final erase-to-end marker arrives.
                while '\x1b[J' in pending[0]:
                    frame, pending[0] = pending[0].split('\x1b[J', 1)
                    stream.feed(frame + '\x1b[J')
            now = time.monotonic()
            if now-last_sample[0] >= .125:
                full = render()
                text = '\n'.join(screen.display)
                for name, heading in (('transfer', 'Moving your browser'), ('scanning', 'Scanning Arc')):
                    if heading in text:
                        score = sum(bool(cell.data.strip()) for y, row in screen.buffer.items() if 6 <= y <= 23
                                    for x, cell in row.items() if 4 <= x <= 89)
                        if name not in best or score > best[name][0]:
                            best[name] = (score, cropped(full))
                image = full.resize((804, 654), Image.Resampling.LANCZOS)
                frames.append(image.quantize(colors=128))
                durations.append(125)
                last_sample[0] = now

    def wait(text):
        deadline = time.monotonic()+30
        while time.monotonic() < deadline:
            if text in '\n'.join(screen.display):
                pump(.15)
                return
            pump(.05)
        raise RuntimeError('Expected synthetic screen not reached: ' + text)

    def cropped(image):
        last = max((y for y, row in screen.buffer.items() if any(c.data.strip() for c in row.values())), default=0)
        return image.crop((0, 0, image.width, min(image.height, PAD*2 + (last+2)*LINE)))

    def save(name):
        image = best[name][1] if name in best else cropped(render())
        image.save(OUT / (name+'.png'))
        if name not in samples: samples.append(name)

    def key(value):
        os.write(fd, value)

    try:
        wait('❯ ArcToZen'); save('startup'); pump(.4)
        wait('Your browsers are ready'); save('browsers'); pump(.8); key(b'\r')
        wait('Scanning Arc'); pump(.25); save('scanning')
        wait('How would you like to migrate?'); pump(.6); key(b'\x1b[B\r')
        wait('Choose what comes with you'); save('options'); pump(1.1)
        key(b'\x1b[A\x1b[A\r')
        wait('Preparing migration')
        wait('Review before replacing Zen'); save('review'); pump(1.2); key(b'\r')
        wait('Moving your browser'); pump(1.25); save('transfer')
        wait('All set.'); save('transfer'); save('scanning'); save('success'); pump(1.3)
        # End without opening Zen or a URL, even though --demo already prevents both.
        key(b'\x1b'); pump(.4)
        _, status = os.waitpid(pid, 0)
        if os.waitstatus_to_exitcode(status): raise RuntimeError('Synthetic wizard did not exit successfully')
        frames[0].save(OUT/'walkthrough.gif', save_all=True, append_images=frames[1:],
                       duration=durations, loop=0, optimize=True, disposal=2)
        print('Captured:', ', '.join(samples), f'; animation: {len(frames)} frames.')
    finally:
        try: os.kill(pid, signal.SIGTERM)
        except ProcessLookupError: pass
        os.close(fd)


if __name__ == '__main__':
    main()
