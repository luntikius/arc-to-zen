"""Layout checks for small terminals and the separate animated splash."""
import io
import os
import unittest
from unittest.mock import patch

from arc_to_zen.terminal import ANSI, Terminal, cells, hero, logo, packet_stream
from arc_to_zen.wizard import review
from types import SimpleNamespace


class TerminalLayoutTests(unittest.TestCase):
    def test_packets_travel_right_then_arrive_without_overflow(self):
        first=packet_stream(0,22,7,compact=True)[3]
        later=packet_stream(.8,22,7,compact=True)[3]
        self.assertGreater(later.index('[·]'),first.index('[·]'))
        self.assertIn('>*<',packet_stream(1.7,22,7,compact=True)[3])
        for width, compact in ((6,True),(14,True),(22,False)):
            for tick in (0,.8,1.7,2.,2.2):
                stream=packet_stream(tick,width,17,colors=True,compact=compact)
                self.assertEqual(len(stream),17)
                self.assertTrue(all(cells(row)==width for row in stream))

    def terminal(self, colors=True):
        ui = Terminal(animation=False, demo=True)
        ui.fd = 0
        ui.output = io.StringIO()
        ui.colors = colors
        return ui

    def test_confirmation_fits_small_terminal_and_keeps_backup_warning(self):
        ui = self.terminal(False)
        result = SimpleNamespace(counts=dict(profiles=2, spaces=3, tabs=42, history=300, logins=9))
        ui.rich = True
        with patch('os.get_terminal_size', return_value=os.terminal_size((50, 18))), patch.object(ui, 'key', return_value='enter'):
            self.assertEqual(review(ui, result, dry_run=True), 'confirm')
        output = ui.output.getvalue()
        self.assertIn('Existing Zen data will be replaced.', output)
        self.assertIn('A full backup will be kept for recovery.', output)
        self.assertIn('❯ Continue migration', output)
        self.assertIn('Back', output)
        self.assertIn('╭─ ArcToZen ', output)
        self.assertNotIn('\x1b[38;', output)
        self.assertLess(len(output.splitlines()), 18)
        for line in output.splitlines():
            line = line.replace('\x1b[H', '').replace('\x1b[K', '').replace('\x1b[J', '')
            self.assertLessEqual(cells(line), 50)

    def test_menus_do_not_render_logos_and_unicode_stays_inside_frame(self):
        ui = self.terminal()
        with patch('os.get_terminal_size', return_value=os.terminal_size((80, 24))), patch('arc_to_zen.terminal.hero', side_effect=AssertionError('Static menu')):
            ui.draw('Your browsers are ready', ['日本語' * 40], [('Continue', 'continue')])
        output = ANSI.sub('', ui.output.getvalue())
        for line in output.splitlines():
            if '│' in line: self.assertEqual(cells(line.replace('\x1b[K', '')), 78)

    def test_rotating_logos_share_angle_and_keep_brand_colors(self):
        with patch('arc_to_zen.terminal.logo', side_effect=lambda kind, angle, w, h, colors: [' '*w]*h) as project:
            hero('transfer', 2, 90)
            self.assertEqual(project.call_args_list[0].args[1], project.call_args_list[1].args[1])
        frame = '\n'.join(hero('intro', 0, 90, colors=True))
        self.assertIn('\x1b[38;2;', frame)
        self.assertNotIn('48;', frame)
        self.assertFalse(set('█▀▄▓▒░') & set(frame))
        self.assertTrue(set('-/\\|:') & set(ANSI.sub('', frame)))
        self.assertNotEqual(hero('transfer', 0, 90), hero('transfer', 2, 90))

    def test_animated_contours_have_empty_centers_and_no_face_fill(self):
        for angle in (0, .5, 1., 2.5):
            for kind in ('arc','zen'):
                frame=logo(kind,angle,60,30,True)
                self.assertEqual(len(frame),30)
                self.assertTrue(all(cells(row)==60 for row in frame))
                text='\n'.join(frame)
                self.assertNotIn('48;',text)
                self.assertFalse(set('█▀▄▓▒░') & set(text))
            if angle != 1.:  # Near edge-on, the ring's side walls can occlude its hole.
                zen=[ANSI.sub('',row) for row in logo('zen',angle,60,30,True)]
                self.assertEqual(zen[14][29:31], '  ')
                self.assertEqual(zen[15][29:31], '  ')

    def test_idle_menu_does_not_redraw_even_with_animation_enabled(self):
        ui = self.terminal()
        ui.rich = ui.animation = True
        with patch('os.get_terminal_size', return_value=os.terminal_size((80, 30))), \
             patch.object(ui, 'key', side_effect=[None, None, 'down', None, 'enter']), \
             patch.object(ui, 'draw', wraps=ui.draw) as draw, \
             patch('arc_to_zen.terminal.hero', side_effect=AssertionError('Interactive screen')):
            result = ui.choose('Review', [], [('Back', 'back'), ('Confirm', 'confirm')], scene='scan')
        self.assertEqual(result, 'confirm')
        self.assertEqual(draw.call_count, 2)

    def test_cards_and_pinned_actions_fit_supported_windows(self):
        for width, height in ((50,18), (60,26), (80,24), (100,32), (100,40)):
            with self.subTest(width=width, height=height):
                ui = self.terminal(False)
                browsers = [dict(kind=k, version='1.0', state='closed', path='/Applications/'+k.title()+'.app') for k in ('arc','zen')]
                with patch('os.get_terminal_size', return_value=os.terminal_size((width,height))), \
                     patch('arc_to_zen.terminal.logo', side_effect=AssertionError('Browser cards have no logos')):
                    ui.draw('Your browsers are ready', [], [('Continue','continue'),('Exit','exit')], browsers=browsers)
                output=ui.output.getvalue()
                self.assertIn('ARC  ✓ Found', output)
                self.assertIn('ZEN  ✓ Found', output)
                self.assertEqual(len(ui.browser_cards(browsers, width-8)), 5)
                self.assertIn('Exit', output)
                self.assertLess(len(output.splitlines()), height)
                ui.output=io.StringIO()
                with patch('os.get_terminal_size', return_value=os.terminal_size((width,height))):
                    ui.draw('Choose what comes with you', ['Profile-level data covers the whole profile.'],
                            [('Select All','all')]+[(f'Option {i}',i) for i in range(30)]+[('Continue to review','continue'),('Back','back')], bottom_actions=2)
                output=ui.output.getvalue()
                self.assertIn('Select All', output)
                self.assertIn('Continue to review', output)
                self.assertIn('Back', output)
                self.assertLess(len(output.splitlines()), height)
                ui.output=io.StringIO();ui.rich=True
                with patch('os.get_terminal_size', return_value=os.terminal_size((width,height))), patch.object(ui,'key',return_value='enter'):
                    review(ui, SimpleNamespace(counts=dict(profiles=2, spaces=3, tabs=36)), True)
                output=ui.output.getvalue()
                self.assertIn('Existing Zen data will be replaced.', output)
                self.assertIn('A full backup will be kept for recovery.', output)
                self.assertLess(len(output.splitlines()), height)

    def test_startup_reveals_name_before_static_interface(self):
        ui = self.terminal(False)
        ui.rich = ui.animation = True
        clock = [0.]
        def advance(_): clock[0] += .3
        with patch('time.monotonic', side_effect=lambda: clock[0]), \
             patch('os.get_terminal_size', return_value=os.terminal_size((80, 30))), \
             patch.object(ui, 'key', side_effect=advance), \
             patch('arc_to_zen.terminal.hero', side_effect=AssertionError('No logos during startup')):
            ui.intro()
        frames = ui.output.getvalue().split('\x1b[H')[1:]
        self.assertNotIn('ArcToZen', frames[0])
        self.assertTrue(any('ArcT▌' in frame for frame in frames))
        self.assertIn('ArcToZen', frames[-1])
        self.assertNotIn('╭─', ui.output.getvalue())
        ui.animation = False
        with patch.object(ui, 'write', side_effect=AssertionError('Animation disabled')):
            ui.intro()


if __name__ == '__main__':
    unittest.main()
