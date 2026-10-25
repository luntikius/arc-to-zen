"""Exercise one-shot installation, cleanup, interruption and corrupt downloads."""
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
assets = pathlib.Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory(prefix='arc-to-zen-bootstrap-') as t:
    base = pathlib.Path(t)
    release = base / 'server/releases/download/v0.4.0'; release.mkdir(parents=True)
    for name in ('arc-to-zen-0.4.0.tar.gz', 'SHA256SUMS'):
        shutil.copyfile(assets / name, release / name)
    script = (root / 'install.sh').read_text().replace(
        'REPOSITORY=https://github.com/luntikius/arc-to-zen', 'REPOSITORY=' + (base / 'server').as_uri())
    script = script.replace("--proto '=https'", "--proto '=https,file'")
    installer = base / 'install.sh'; installer.write_text(script)
    blocked = base / 'bin'; blocked.mkdir()
    for name in ('python', 'python3', 'uv', 'brew', 'sudo'):
        p = blocked / name; p.write_text('#!/bin/sh\nexit 99\n'); p.chmod(0o700)
    sessions = base / 'sessions'; sessions.mkdir()
    old = base / 'previous-install'; old.mkdir(); (old / 'keep').write_text('existing tool')
    recovery = base / 'recovery'; recovery.mkdir(); (recovery / 'keep').write_text('backup')
    env = {**os.environ, 'PATH': str(blocked) + ':/usr/bin:/bin:/usr/sbin:/sbin',
           'ARC_TO_ZEN_HOME': str(old), 'TMPDIR': str(sessions) + '/'}
    def retained():
        assert not list(sessions.iterdir()), 'One-shot files remain'
        assert (old / 'keep').read_text() == 'existing tool'
        assert (recovery / 'keep').read_text() == 'backup'
    for _ in range(0 if "--interrupt-only" in sys.argv else 2):
        result = subprocess.run(['/bin/sh', str(installer), '--help'], env=env, capture_output=True, text=True)
        if result.returncode: print(result.stderr); result.check_returncode()
        assert 'Temporary tool removed.' in result.stdout
        retained()
    if "--interrupt-only" not in sys.argv:
        (release / 'SHA256SUMS').write_text('0' * 64 + '  arc-to-zen-0.4.0.tar.gz\n')
        bad = subprocess.run(['/bin/sh', str(installer), '--help'], env=env, capture_output=True, text=True)
        assert bad.returncode != 0 and 'Release checksum mismatch' in bad.stderr
        retained()
    # Interrupt after ownership has been recorded, before any download/runtime.
    installer.write_text(script.replace("printf '%s\\n' 'Preparing a private one-shot ArcToZen runtime…'",
        "/bin/sleep 30 & printf 'INTERRUPT_READY %s\\n' \"$!\"; wait"))
    interrupted = subprocess.Popen(['/bin/sh', str(installer), '--help'], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        ready = interrupted.stdout.readline().strip().split()
        assert ready[0] == 'INTERRUPT_READY'
        sleeper = int(ready[1])
        os.killpg(interrupted.pid, signal.SIGTERM)
        interrupted.wait(timeout=10)
        assert interrupted.returncode != 0
    finally:
        if interrupted.poll() is None: os.killpg(interrupted.pid, signal.SIGKILL); interrupted.wait()
        try: os.kill(sleeper, signal.SIGKILL)
        except ProcessLookupError: pass
        interrupted.stdout.close(); interrupted.stderr.close()
    retained()
    print('One-shot bootstrap checks passed' + (' (interruption only).' if '--interrupt-only' in sys.argv else ': clean installs, corrupt checksum, interruption and retention.'))
