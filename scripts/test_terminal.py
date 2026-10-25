"""Exercise the real terminal UI with synthetic data in a controlling PTY."""
import os,pty,fcntl,termios,struct,select,time,signal,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
pid,fd=pty.fork()
if pid==0:
 os.environ['TERM']='xterm-256color'
 os.environ['PYTHONPATH']=str(root)
 os.environ['PYTHONDONTWRITEBYTECODE']='1'
 os.execv(sys.executable,[sys.executable,'-m','arc_to_zen.cli','wizard','--demo'] + ([] if '--animated' in sys.argv else ['--no-animation']) + (['--restore'] if '--restore' in sys.argv else []))
fcntl.ioctl(fd,termios.TIOCSWINSZ,struct.pack('HHHH',40,100,0,0))
log=b'';mark=0

def wait(text,timeout=20):
 global log,mark
 end=time.monotonic()+timeout
 while time.monotonic()<end:
  if text.encode() in log[mark:]:mark=log.index(text.encode(),mark)+len(text.encode());return
  if select.select([fd],[],[],.1)[0]:
   try:chunk=os.read(fd,65536)
   except OSError:break
   if not chunk:break
   log+=chunk
 raise AssertionError('Missing screen: '+text+'; tail='+repr(log[-250:]))
try:
 if '--animated' in sys.argv:
  wait('Bring your browser with you.')
  assert '╭─'.encode() not in log
  os.write(fd,b'\r')
 if '--restore' in sys.argv:
  wait('Restore from backup')
  os.write(fd,b'\x1b[B\r');wait('Confirm restore')
  wait('Restore despite later changes')
  os.write(fd,b'\r');wait('Restore from backup')
  os.write(fd,b'\x1b[B\r');wait('Confirm restore')
  os.write(fd,b'\x1b[B\r');wait('Restoring Zen');wait('Restore complete.')
  os.write(fd,b'\r');wait('\x1b[?25h\x1b[?1049l')
 else:
  wait('Your browsers are ready');os.write(fd,b'\r')
  assert b'01 / 05  Your browsers are ready' in log
  wait('Scanning Arc');wait('How would you like to migrate?')
  os.write(fd,b'\x1b[B');time.sleep(.08);os.write(fd,b'\r')
  assert '╭─ ArcToZen '.encode() in log
  wait('Choose what comes with you')
  # Resizing must not accept a destructive action in an unreadable viewport.
  fcntl.ioctl(fd,termios.TIOCSWINSZ,struct.pack('HHHH',12,30,0,0));wait('Resize to at least')
  os.write(fd,b'\r');time.sleep(.25)
  fcntl.ioctl(fd,termios.TIOCSWINSZ,struct.pack('HHHH',40,100,0,0));wait('Choose what comes with you')
  # Select All is first; pinned confirmation actions are last.
  os.write(fd,b'\x1b[A\x1b[A');time.sleep(.08);os.write(fd,b'\r')
  wait('Preparing migration');wait('Review before replacing Zen')
  os.write(fd,b'\x1b[B');time.sleep(.08);os.write(fd,b'\r');wait('Scan details')
  os.write(fd,b'\r');wait('Review before replacing Zen')
  os.write(fd,b'\r')
  wait('Moving your browser');wait('All set.')
  os.write(fd,b'\r');wait('\x1b[?25h\x1b[?1049l')
 _,status=os.waitpid(pid,0);assert os.waitstatus_to_exitcode(status)==0
 assert b'\x1b[?25h\x1b[?1049l' in log
 print('PTY: backup selection, cancellation, explicit restore confirmation and completion passed.' if '--restore' in sys.argv else 'PTY: selection, resize, scan details, confirmation, transfer, success and terminal restoration passed.')
finally:
 if '--record' in sys.argv:
  Path(sys.argv[sys.argv.index('--record')+1]).write_bytes(log)
 try:os.kill(pid,signal.SIGTERM)
 except ProcessLookupError:pass
 os.close(fd)
