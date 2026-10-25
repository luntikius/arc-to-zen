"""Small terminal renderer: perspective logos, menus and honest worker progress."""
import math
from functools import lru_cache
import os
import queue
import re
import select
import termios
import threading
import time
import tty
import unicodedata
from .common import MigrationError, safe
from .transfer import Cancelled

RESET = '\033[0m'
PURPLE = '38;5;141'
MUTED = '38;5;245'
BORDER = '38;5;98'
ANSI = re.compile(r'\x1b\[[0-9;]*m')


def cells(value):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
               for c in ANSI.sub('', value))


def clipped(value, width):
    result = ''
    for c in safe(value):
        if cells(result + c) > width: return clipped(result, width - 1) + '…'
        result += c
    return result



@lru_cache(maxsize=2)
def logo_surfaces(kind):
    """Extruded ribbons: faces hide rear edges, only perimeter edges are drawn."""
    surfaces, edges = [], []
    def ribbon(curve, rgb, layer=0., radius=.12, closed=False):
        left, right = [], []
        steps = 48
        for i in range(steps + 1):
            t = i / steps
            x, y = curve(t)
            ax, ay = curve(max(0., t-.001))
            bx, by = curve(min(1., t+.001))
            length = math.hypot(bx-ax, by-ay)
            nx, ny = -(by-ay)/length*radius, (bx-ax)/length*radius
            left.append((x+nx, y+ny)); right.append((x-nx, y-ny))
        front = [[(x,y,layer+.12) for x,y in side] for side in (left,right)]
        back = [[(x,y,layer-.12) for x,y in side] for side in (left,right)]
        def face(a,b,c,d): surfaces.extend(((a,b,c),(a,c,d)))
        for i in range(steps):
            for side in (front,back):
                face(side[0][i],side[0][i+1],side[1][i+1],side[1][i])
                for boundary in side: edges.append((boundary[i],boundary[i+1],rgb,False))
            for side in (0,1):
                face(front[side][i],front[side][i+1],back[side][i+1],back[side][i])
        if not closed:
            for i in (0,steps):
                face(front[0][i],front[1][i],back[1][i],back[0][i])
                for side in (front,back): edges.append((side[0][i],side[1][i],rgb,False))
        for i in ((0,12,24,36) if closed else (0,steps)):
            for side in (0,1): edges.append((front[side][i],back[side][i],rgb,True))
    if kind == 'arc':
        ribbon(lambda t: (-.65+.65*t,-.85+1.7*t), (28,0,120), -.06)
        ribbon(lambda t: (-.3+.3*t,.05+.8*t), (255,74,110), -.03)
        def curve(t):
            u=1-t
            return (u**3*(-.83)+3*u*u*t*(-.5)+3*u*t*t*.65+t**3*.98,
                    u**3*.05+3*u*u*t*(-.87)+3*u*t*t*(-.72)+t**3*.35)
        ribbon(curve,(255,139,150))
        ribbon(lambda t: curve(t*.58),(0,38,216),.04)
        ribbon(lambda t: (.01+.63*t,.86-1.72*t),(255,74,110),.08)
    else:
        for radius in (.34,.69,1.04):
            ribbon(lambda t,r=radius:(r*math.cos(t*math.tau),r*math.sin(t*math.tau)),
                   (246,111,81),radius=.055,closed=True)
    return surfaces, edges


def logo(kind, angle, width=24, height=11, colors=False):
    """Hidden-line rendering: empty faces, colored contours and depth edges."""
    surfaces, edges = logo_surfaces(kind)
    cosine, sine = math.cos(angle), math.sin(angle)
    def project(point):
        x,y,z=point
        depth=-x*sine+z*cosine
        scale=3.8/(3.8-depth)
        return ((width-1)/2+(x*cosine+z*sine)*scale*width*.32,
                (height-1)/2-y*scale*height*.37,depth)
    depths=[[-99.]*width for _ in range(height)]
    for triangle in surfaces:
        a,b,c=map(project,triangle)
        denominator=(b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1])
        if abs(denominator)<1e-8: continue
        for y in range(max(0,math.ceil(min(a[1],b[1],c[1]))),min(height-1,math.floor(max(a[1],b[1],c[1])))+1):
            for x in range(max(0,math.ceil(min(a[0],b[0],c[0]))),min(width-1,math.floor(max(a[0],b[0],c[0])))+1):
                u=((b[1]-c[1])*(x-c[0])+(c[0]-b[0])*(y-c[1]))/denominator
                v=((c[1]-a[1])*(x-c[0])+(a[0]-c[0])*(y-c[1]))/denominator
                if u>=0 and v>=0 and u+v<=1:
                    depths[y][x]=max(depths[y][x],u*a[2]+v*b[2]+(1-u-v)*c[2])
    grid=[[' ']*width for _ in range(height)]
    ink=[[-99.]*width for _ in range(height)]
    for start,end,rgb,connector in edges:
        a,b=project(start),project(end)
        dx,dy=b[0]-a[0],b[1]-a[1]
        if connector and (abs(sine)<.2 or math.hypot(dx,dy)<.8): continue
        count=max(1,math.ceil(max(abs(dx),abs(dy))))
        char='-' if abs(dx)>2*abs(dy) else '|' if abs(dy)>2*abs(dx) else '/' if dx*dy<0 else '\\'
        for i in range(count+1):
            t=i/count; x=round(a[0]+dx*t); y=round(a[1]+dy*t); depth=a[2]+(b[2]-a[2])*t
            if not (0<=x<width and 0<=y<height): continue
            if depth<depths[y][x]-.04 or depth<ink[y][x]: continue
            value=char
            if colors:
                light=max(.6,min(1.,.85+depth*.12))
                shaded=tuple(round(v*light) for v in rgb)
                value='\033[38;2;%d;%d;%dm%s%s' % (*shaded,value,RESET)
            grid[y][x]=value; ink[y][x]=depth
    return [''.join(row) for row in grid]


def packet_stream(tick, width, height, colors=False, compact=False):
    """Decorative packet motion; actual progress stays in the worker status."""
    grid = [[(' ', PURPLE) for _ in range(width)] for _ in range(height)]
    lanes = [height//2] if compact else [height//2-3, height//2, height//2+3]
    packet = '[·]' if compact else '[::]'
    for lane, row in enumerate(lanes):
        phase = (tick + lane*.67) % 2.1
        if phase < 1.65:
            progress = phase/1.65
            position = round((width-len(packet))*progress**.85)
            # A fading tail keeps direction readable between terminal frames.
            for offset, char in enumerate('·──'):
                x = position-3+offset
                if x >= 0: grid[row][x] = (char, BORDER if offset < 2 else PURPLE)
            for offset, char in enumerate(packet):
                grid[row][position+offset] = (char, '1;38;5;217' if progress > .75 else '1;'+PURPLE)
        elif phase < 1.95:
            # A short receipt pulse replaces the packet when it reaches Zen.
            pulse = ' + ' if phase > 1.8 else '>*<'
            for offset, char in enumerate(pulse):
                grid[row][width-3+offset] = (char, '1;38;5;209')
            if not compact and phase < 1.8:
                grid[row-1][width-2] = ('·', PURPLE)
                grid[row+1][width-2] = ('·', PURPLE)
    return [''.join(('\033['+code+'m'+char+RESET) if colors and char != ' ' else char
                    for char,code in row) for row in grid]


def hero(scene, tick, width, compact=False, colors=False):
    gap = 22 if width >= 78 else 14 if width >= 58 else 6
    w = min(18 if compact else 35, (width-gap)//2)
    h = 7 if compact else min(17, (w+1)//2)
    angle = tick * .8
    def center(line): return ' ' * max(0, (width - cells(line)) // 2) + line
    if scene == 'scan':
        lines = logo('arc', angle, w, h, colors)
        return [center(l) for l in lines] + ['ARC'.center(width)]
    left = logo('arc', angle, w, h, colors)
    right = logo('zen', angle, w, h, colors)
    packets = packet_stream(tick, gap, h, colors, compact) if scene == 'transfer' else None
    streams = []
    for row in range(h):
        stream = packets[row] if packets else ('>'.center(gap) if row == h//2 else ' '*gap)
        streams.append(center(left[row] + stream + right[row]))
    streams.append(('ARC'.center(w) + ' ' * gap + 'ZEN'.center(w)).center(width))
    return streams


class Terminal:
    def __init__(self, animation=True, demo=False):
        self.animation, self.demo = animation, demo
        self.file = self.saved = None
        self.rich = False
        self.colors = 'NO_COLOR' not in os.environ
        self.started = time.monotonic()
        self.receipt = []
        self.input_buffer = b''

    def __enter__(self):
        try:
            self.file = open('/dev/tty', 'r')
            self.output = open('/dev/tty', 'w', buffering=1)
        except OSError:
            if self.file: self.file.close()
            raise MigrationError('An interactive terminal is required. No browser data changed. Run from Terminal.') from None
        self.fd = self.file.fileno()
        self.rich = os.isatty(self.fd) and os.environ.get('TERM', '') not in ('', 'dumb')
        if self.rich:
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            self.write('\033[?1049h\033[?25l')
        return self

    def __exit__(self, *exc):
        if self.rich:
            self.write(RESET + '\033[?25h\033[?1049l')
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        if self.receipt:
            self.write("\n" + "\n".join(safe(line) for line in self.receipt) + "\n")
        self.file.close(); self.output.close()

    def write(self, value):
        self.output.write(value); self.output.flush()

    def color(self, value, code):
        return '\033[' + code + 'm' + value + RESET if self.colors else value

    def key(self, timeout=.1):
        if not self.input_buffer:
            if not select.select([self.fd], [], [], timeout)[0]: return None
            self.input_buffer = os.read(self.fd, 64)
        if self.input_buffer.startswith(b'\x1b'):
            if len(self.input_buffer) < 3 and select.select([self.fd], [], [], .015)[0]:
                self.input_buffer += os.read(self.fd, 64)
            for sequence, value in ((b'\x1b[A', 'up'), (b'\x1b[B', 'down')):
                if self.input_buffer.startswith(sequence):
                    self.input_buffer = self.input_buffer[len(sequence):]
                    return value
        key, self.input_buffer = self.input_buffer[:1], self.input_buffer[1:]
        return {b'\r': 'enter', b'\n': 'enter', b' ': 'space', b'\x1b': 'back',
                b'\x03': 'cancel', b'q': 'cancel'}.get(key, key.decode(errors='ignore'))

    def browser_cards(self, browsers, width):
        card_width = (width - 2) // 2
        content = card_width - 4
        def card(browser):
            name = browser['kind'].upper()
            def line(text):
                return self.color('│ ', BORDER) + text + ' ' * (content - cells(text)) + self.color(' │', BORDER)
            rows = [self.color('╭' + '─' * (card_width - 2) + '╮', BORDER),
                    line(self.color(clipped(name + '  ✓ Found', content), '1;' + PURPLE)),
                    line(clipped(browser['version'] + ' · ' + browser['state'], content)),
                    line(clipped(browser['path'], content))]
            return rows + [self.color('╰' + '─' * (card_width - 2) + '╯', BORDER)]
        left, right = map(card, browsers)
        return [a + '  ' + b for a, b in zip(left, right)]

    def metric_cards(self, metrics, width, height):
        columns = 3 if width >= 66 else 2
        w = (width - (columns - 1) * 2) // columns
        if height < 26 or (columns == 2 and height < 30):
            return [self.color(clipped(' · '.join(f'{n:,} {label}' for label, n in metrics[i:i+columns]), width), PURPLE)
                    for i in range(0, len(metrics), columns)]
        rows = []
        for i in range(0, len(metrics), columns):
            group = metrics[i:i+columns]
            rows += [self.color('  '.join('╭'+'─'*(w-2)+'╮' for _ in group), BORDER),
                     '  '.join(self.color('│ ', BORDER) + self.color(clipped(f'{n:,}', w-4).ljust(w-4), '1;'+PURPLE) + self.color(' │', BORDER) for _, n in group),
                     self.color('  '.join('│ '+clipped(label,w-4).ljust(w-4)+' │' for label, _ in group), MUTED),
                     self.color('  '.join('╰'+'─'*(w-2)+'╯' for _ in group), BORDER)]
        return rows

    def draw(self, title, lines, options=(), cursor=0, stage=1, scene='intro', status='', browsers=(), metrics=(), bottom_actions=0):
        size = os.get_terminal_size(self.fd)
        width, height = min(96, size.columns - 4), size.lines
        if size.columns < 50 or height < 18:
            self.write('\033[H\033[JArcToZen\nResize to at least 50 columns × 18 rows.\nCtrl+C cancels safely.\n')
            return False
        inner = width - 4
        tick = time.monotonic() - self.started if self.animation else 0
        def row(text='', code=None):
            if code: text = self.color(clipped(text, inner), code)
            return self.color('│ ', BORDER) + text + ' ' * max(0, inner - cells(text)) + self.color(' │', BORDER)
        heading = ' ArcToZen '
        rows = [self.color('╭─' + heading + '─' * (width - len(heading) - 3) + '╮', BORDER)]
        mode = 'LOCAL MIGRATION · ARC → ZEN'
        rows += [row(mode, MUTED), row(), row(f'{stage:02d} / 05  {safe(title)}', '1;' + PURPLE), row()]
        if browsers: rows += [row(line) for line in self.browser_cards(browsers, inner)]
        if metrics: rows += [row(line) for line in self.metric_cards(metrics, inner, height)]
        # Menus stay still. Animated logos are reserved for active operations.
        if status and not options and scene in ('scan', 'transfer') and height >= 26:
            compact = height < 38 or inner < 70
            rows += [row(line) for line in hero(scene, tick, inner, compact, self.colors)]
            rows += [row()]
        # Reserve the footer, all confirmation choices and the progress line.
        room = max(0, height - len(rows) - 5 - min(4, len(options)) - bool(status))
        for line in lines[:room]: rows.append(row(line, MUTED))
        if len(lines) > room and room:
            rows[-1] = row('… enlarge the window for the full summary', MUTED)
        if status: rows.append(row('● ' + safe(status), PURPLE))
        rows.append(row())
        available = max(1, height - len(rows) - 3 - bottom_actions - bool(bottom_actions))
        body_count = len(options) - bottom_actions
        start = max(0, min(min(cursor, body_count - 1) - available // 2, body_count - available))
        end = min(body_count, start + available)
        def option(index):
            text = ('❯ ' if index == cursor else '  ') + safe(options[index][0])
            rows.append(row(text, '1;' + PURPLE if index == cursor else '39'))
        for index in range(start, end): option(index)
        if bottom_actions:
            rows.append(row('─' * inner, BORDER))
            for index in range(body_count, len(options)): option(index)
        scroll = f' {start + 1}–{end} / {body_count} ' if body_count > available else ''
        rows.append(self.color('╰' + '─' * (width - len(scroll) - 2) + scroll + '╯', BORDER))
        controls = 'Ctrl+C · cancel at the next safe boundary'
        if options:
            controls = '↑↓ navigate · space/enter select · esc back · ctrl+c cancel'
            if width < 62: controls = '↑↓ move · enter select · esc back · ^C cancel'
        rows += [self.color(clipped(controls, width), MUTED)]
        self.write('\033[H' + '\n'.join('  ' + line + '\033[K' for line in rows) + '\033[J')
        return True

    def intro(self):
        if not self.rich or not self.animation: return
        started = time.monotonic()
        while time.monotonic() - started < 2.4:
            size = os.get_terminal_size(self.fd)
            width = min(96, size.columns - 4)
            if width < 46 or size.lines < 18: return
            elapsed = time.monotonic() - started
            name = 'ArcToZen'
            revealed = min(len(name), int(elapsed / .13))
            # Reserve the entire word's width so the title stays in place as it appears.
            title = '❯ ' + (name[:revealed] + ('▌' if revealed < len(name) else ' ')).ljust(len(name) + 1)
            rows = [self.color(title.center(width), '1;' + PURPLE)]
            rows += ['', self.color('Bring your browser with you.'.center(width), PURPLE)]
            top = max(0, (size.lines - len(rows) - 1) // 2)
            self.write('\033[H' + '\n' * top + '\n'.join('  ' + line + '\033[K' for line in rows) + '\033[J')
            key = self.key(1 / 12)
            if key == 'cancel': raise KeyboardInterrupt
            if key: break

    def choose(self, title, lines, options, stage=2, scene='intro', cursor=0, browsers=(), metrics=(), bottom_actions=0):
        if not self.rich:
            lines = list(lines) + [f"✓ {b['kind'].title()} {b['version']} · {b['state']} · {b['path']}" for b in browsers]
            lines += [f'{label}: {n:,}' for label, n in metrics]
            self.write('\n' + safe(title) + '\n' + '\n'.join(safe(x) for x in lines) + '\n')
            for i, (label, _) in enumerate(options, 1): self.write(f' {i}. {safe(label)}\n')
            while True:
                self.write('Choose a number (q to cancel): ')
                answer = self.file.readline().strip()
                if answer.lower() == 'q' or not answer: return 'cancel'
                if answer.isdigit() and 1 <= int(answer) <= len(options): return options[int(answer) - 1][1]
        previous_view = None
        while True:
            view = (cursor, os.get_terminal_size(self.fd))
            if view != previous_view:
                usable = self.draw(title, lines, options, cursor, stage, scene,
                                   browsers=browsers, metrics=metrics, bottom_actions=bottom_actions)
                previous_view = view
            key = self.key(.2)
            if not usable:
                if key in ('back', 'cancel'): return key
                continue
            if key == 'up': cursor = (cursor - 1) % len(options)
            elif key == 'down': cursor = (cursor + 1) % len(options)
            elif key in ('enter', 'space'): return options[cursor][1]
            elif key in ('back', 'cancel'): return key

    def work(self, title, operation, stage, scene):
        updates, done = queue.Queue(), queue.Queue()
        cancelled = threading.Event()
        def emit(message, count=None, total=None):
            if cancelled.is_set(): raise Cancelled('Cancelled before the next safe operation.')
            updates.put(message + (f'  {count}/{total}' if total is not None else ''))
        def worker():
            try: done.put((True, operation(emit)))
            except BaseException as exc: done.put((False, exc))
        thread = threading.Thread(target=worker, name='arc-to-zen-worker')
        thread.start(); status = 'Preparing…'; last = None
        try:
            while thread.is_alive():
                try:
                    while True: status = updates.get_nowait()
                except queue.Empty: pass
                if self.rich:
                    self.draw(title, ['Ctrl+C requests cancellation at the next safe boundary.',
                        'macOS may request Keychain access for selected encrypted data.'] if stage == 4 else
                        ['Counts for encrypted records are provisional until transfer.'] if title == 'Preparing migration' else [],
                        stage=stage, scene=scene,
                        status='Cancelling safely…' if cancelled.is_set() else status)
                    try:
                        if self.key(1 / 12 if self.animation else .2) == 'cancel': cancelled.set()
                    except KeyboardInterrupt: cancelled.set()
                else:
                    if status != last: self.write(safe(status) + '\n'); last = status
                    try: thread.join(.1)
                    except KeyboardInterrupt: cancelled.set()
        finally:
            # Never leave a background installer alive after the UI exits.
            if thread.is_alive(): cancelled.set(); thread.join()
        success, value = done.get()
        if not success: raise value
        return value
