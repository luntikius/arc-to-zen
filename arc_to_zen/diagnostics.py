"""Opt-in, allowlisted diagnostics. Never serialize browser records or exceptions."""
from __future__ import annotations

import json
import math
import os
import platform
import re
import threading
import time
import uuid
import functools
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from .common import MigrationError

_active = None  # The wizard's worker thread shares the invocation's recorder.
_package = Path(__file__).parent.resolve()
REASONS = {
    'partition_ancestor', 'source_type', 'source_scheme', 'expired', 'partition_format',
    'partition_port', 'cipher_unsupported', 'encoding', 'same_site', 'invalid_domain',
    'empty_name_value', 'oversize', 'invalid_name', 'invalid_value', 'invalid_path',
    'invalid_prefix', 'none_requires_secure', 'partition_requires_secure',
    'httponly_from_script', 'secure_non_https', 'non_same_site', 'path_oversize',
    'domain_oversize', 'expiry_oversize', 'unknown_schema', 'conversion',
    'cookie missing', 'cookie value', 'cookie security', 'cookie session', 'cookie isolation',
    'cookie expiry', 'cookie host API', 'cookie API failure', 'cookie validation API',
    'appearance capability', 'appearance missing object', 'appearance image', 'appearance icon',
    'appearance controls', 'appearance gradient', 'appearance texture', 'appearance folder',
    'appearance background', 'appearance palette', 'space indicator', 'login', 'unsupported data or API',
}
OPERATIONS = {'identity', 'capabilities', 'containers', 'verifyContainers', 'verifyUI',
    'appearanceCapabilities', 'defaultTheme', 'applyAppearance', 'verifyAppearance',
    'export', 'profileGroup', 'history', 'logins', 'cookies', 'bookmarks', 'forms',
    'addresses', 'cards', 'verifyData', 'state', 'snapshot', 'shutdown', 'keychain',
    'discovery', 'scan', 'transfer', 'command', 'theme', 'source', 'native', 'transaction', 'sidebar'}
CATEGORIES = {'tabs', 'themes', 'icons', 'history', 'archive', 'passwords', 'cookies',
              'bookmarks', 'forms', 'addresses', 'cards'}
EVENTS = {'session_start', 'session_end', 'invocation', 'browser', 'selection', 'phase', 'run',
          'cookie_read', 'cookie_skip', 'cookie_batch', 'cookie_summary', 'theme',
          'space', 'native_call', 'error', 'verification', 'transaction', 'scan_summary', 'transfer_summary'}
ENUMS = {
    'operation': OPERATIONS, 'stage': {'source', 'native', 'scan', 'transfer', 'restart', 'mapping'},
    'reason': REASONS, 'status': {'started', 'ok', 'failed', 'cancelled', 'absent',
        'unsupported', 'converted', 'default', 'verified', 'incomplete'},
    'browser': {'arc', 'zen'}, 'category': CATEGORIES,
    'architecture': {'arm64', 'x86_64'},
    'step': {'backup', 'install', 'verify'},
    'phase': {'preparing', 'applying', 'installed', 'failed', 'rolling_back', 'rolled_back'},
    'exception': {'MigrationError', 'KeyboardInterrupt', 'OSError', 'PermissionError',
        'FileNotFoundError', 'ValueError', 'TypeError', 'KeyError', 'TimeoutExpired',
        'TimeoutException', 'JavascriptException', 'OperationalError', 'DatabaseError',
        'AttributeError', 'IndexError', 'RuntimeError', 'AssertionError', 'ImportError'},
}
NUMBERS = {'elapsed_ms', 'profile', 'space', 'container', 'tab', 'cookie', 'restart',
           'source', 'accepted', 'skipped', 'tabs', 'spaces', 'profiles', 'exit_code',
           'source_skipped', 'native_skipped', 'themes', 'icons', 'done', 'total', 'line'}
BOOLS = {'matches', 'default_container', 'indicator_hidden', 'hide_default_indicator',
         'theme_present', 'new_tab', 'dry_run', 'demo', 'enabled'}
THEME_KEYS = set('background single dual light dark automatic system _0 style color solid '
    'gradient blendedGradient baseColors overlayColors red green blue alpha colorSpace '
    'contentOverBackgroundAppearance isVibrant translucencyStyle modifiers intensityFactor '
    'noiseFactor overlay wheel analogous complementary floating singleAnalogous triadic '
    'splitComplementary monochromatic primaryColorPalette midTone shaded shadedDark tintedLight '
    'semanticColorPalette appearanceBased maxContrastColor title foregroundPrimary '
    'backgroundExtra cutoutColor subtitle minContrastColor focus foregroundTertiary hover '
    'foregroundSecondary type gradientColors c isCustom algorithm isPrimary lightness '
    'opacity texture'.split())
THEME_WORDS = set('sRGB extendedSRGB displayP3 displayP3ColorSpace light dark automatic system '
    'grain none gradient floating singleAnalogous analogous complementary triadic '
    'splitComplementary monochromatic explicit-lightness explicit-blackwhite'.split())


def theme_shape(value, depth=0):
    """Keep only structural theme keys and numeric color/control values."""
    if depth > 16:
        return {'truncated': True}
    if isinstance(value, dict):
        result = {k: theme_shape(v, depth + 1) for k, v in value.items() if k in THEME_KEYS}
        if len(result) != len(value): result['unknown_fields'] = len(value) - len(result)
        return result
    if isinstance(value, list):
        return [theme_shape(v, depth + 1) for v in value[:16]]
    if value is None or type(value) is bool: return value
    if type(value) in (int, float): return value if abs(value) <= 10000 and math.isfinite(value) else None
    if isinstance(value, str): return value if value in THEME_WORDS else '<unknown-string>'
    return '<unknown-type>'


def _fields(fields):
    result = {}
    for key, value in fields.items():
        if key in NUMBERS and type(value) in (int, float) and math.isfinite(value): result[key] = value
        elif key in BOOLS and type(value) is bool: result[key] = value
        elif key in ENUMS: result[key] = value if isinstance(value, str) and value in ENUMS[key] else 'unknown'
        elif key in ('version', 'macos', 'python') and isinstance(value, str):
            if re.fullmatch(r'[0-9][0-9.a-z+-]{0,39}', value): result[key] = value
        elif key == 'run' and isinstance(value, str) and re.fullmatch(r'\d{8}T\d{6}Z-[a-f0-9]{8}', value): result[key] = value
        elif key == 'categories': result[key] = sorted(set(value) & CATEGORIES)
        elif key in ('input_theme', 'output_theme'): result[key] = theme_shape(value)
        elif key == 'field' and isinstance(value, str) and value in THEME_KEYS: result[key] = value
        elif key == 'file' and value in {'cli.py', 'wizard.py', 'transfer.py', 'source.py', 'zen.py',
                'appearance.py', 'transaction.py', 'crypto.py', 'platforms.py', 'common.py',
                'repair.py', 'repair_appearance.py', 'merge.py', 'sessions.py', 'recovery.py'}:
            result[key] = value
        elif key == 'reasons': result[key] = {k: v for k, v in value.items() if k in REASONS and type(v) is int}
        elif key in ('counts', 'skipped_counts'):
            keys = CATEGORIES | {'logins', 'profiles', 'spaces', 'folders', 'appearance', 'themeApproximations'}
            result[key] = {k: v for k, v in value.items() if k in keys and type(v) is int and v >= 0}
    return result


class Recorder:
    def __init__(self, runtime):
        self.path = Path(runtime) / 'diagnostics' / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex + '.jsonl')
        self.lock = threading.RLock()
        self.started = time.monotonic()
        self.failed = False
        self.ids = {}
        self.next_cookie = 0
        self.cookie_counts = Counter()
        self.reasons = Counter()
        self.run = None
        self.file = None
        try:
            runtime = Path(runtime)
            if runtime.is_symlink() or self.path.parent.is_symlink(): raise OSError()
            runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path.parent.mkdir(exist_ok=True, mode=0o700)
            fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                if os.fstat(fd).st_uid != os.getuid(): raise OSError()
                os.fchmod(fd, 0o700)
                out = os.open(self.path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            finally:
                os.close(fd)
            self.file = os.fdopen(out, 'w', encoding='utf-8')
            self.emit('session_start', macos=platform.mac_ver()[0], python=platform.python_version(), architecture=platform.machine())
            if self.failed: raise OSError()
        except OSError:
            if self.file: self.file.close()
            raise MigrationError('Cannot create diagnostic log; migration has not started.') from None

    def alias(self, kind, value):
        with self.lock:
            ids = self.ids.setdefault(kind, {})
            if value not in ids: ids[value] = len(ids) + 1
            return ids[value]

    def emit(self, event, **fields):
        if event not in EVENTS: return
        with self.lock:
            if self.failed: return
            try:
                row = {'schema': 1, 'event': event, 'elapsed_ms': round((time.monotonic() - self.started) * 1000), **_fields(fields)}
                if self.run: row['run'] = self.run
                self.file.write(json.dumps(row, ensure_ascii=True, allow_nan=False) + '\n')
                self.file.flush()
            except (OSError, ValueError, TypeError, OverflowError, RecursionError):
                self.failed = True

    def close(self):
        if self.file.closed: return
        try:
            self.file.flush()
            os.fsync(self.file.fileno())
        except OSError:
            self.failed = True
        finally:
            try: self.file.close()
            except OSError: self.failed = True


@contextmanager
def recording(runtime, enabled):
    global _active
    previous = _active
    recorder = Recorder(runtime) if enabled else None
    _active = recorder
    try:
        yield recorder
    finally:
        if recorder: recorder.close()
        _active = previous


def emit(event, **fields):
    if _active: _active.emit(event, **fields)


def traced(operation):
    def decorate(function):
        @functools.wraps(function)
        def run(*args, **kwargs):
            started = time.monotonic()
            emit('phase', operation=operation, status='started')
            try:
                result = function(*args, **kwargs)
            except BaseException as exc:
                error(operation, exc)
                raise
            emit('phase', operation=operation, status='ok', elapsed_ms=round((time.monotonic() - started) * 1000))
            return result
        return run
    return decorate


def alias(kind, value):
    return _active.alias(kind, value) if _active else 0


def bind_run(run):
    if _active:
        _active.run = _fields({'run': run}).get('run')
        _active.cookie_counts.clear()
        _active.reasons.clear()
        emit('run', status='started')


def cookie_read(profile):
    if not _active: return 0
    with _active.lock:
        _active.next_cookie += 1
        _active.cookie_counts['source'] += 1
        ident = _active.next_cookie
        emit('cookie_read', cookie=ident, profile=alias('profile', profile), stage='source')
        return ident


def cookie_skip(ident, stage, reason):
    if not _active: return
    with _active.lock:
        _active.cookie_counts[stage + '_skipped'] += 1
        _active.reasons[reason] += 1
        emit('cookie_skip', cookie=ident, stage=stage, reason=reason)


def cookie_summary(accepted):
    if _active:
        emit('cookie_summary', **_active.cookie_counts, accepted=accepted,
             skipped=sum(_active.reasons.values()), reasons=dict(_active.reasons))


def error(operation, exc):
    emit('error', operation=operation, exception=type(exc).__name__, status='failed')
    # No exception message, chained exceptions, source lines, arguments or locals.
    if _active and not _active.failed:
        tb = exc.__traceback__
        while tb:
            code = tb.tb_frame.f_code
            path = Path(code.co_filename)
            if path.parent.resolve() == _package:
                emit('error', operation=operation, file=path.name, line=tb.tb_lineno)
            tb = tb.tb_next


def receipt():
    if not _active: return []
    return [('Diagnostic log incomplete: ' if _active.failed else 'Diagnostic log: ') + str(_active.path)]
