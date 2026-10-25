"""Build a public archive from an explicit allowlist, never workspace history/data."""
import hashlib
import pathlib
import sys
import tarfile

root = pathlib.Path(__file__).resolve().parents[1]
version = '0.4.2rc1'
out = pathlib.Path(sys.argv[1]).resolve()
out.mkdir(parents=True, exist_ok=True)
archive = out / f'arc-to-zen-{version}.tar.gz'
files = []
for name in ('arc_to_zen', 'tests', 'scripts', '.github', 'README.md',
             'LICENSE', 'docs/assets', 'docs/INTERFACE.md', 'demo.sh', 'pyproject.toml', 'uv.lock', 'install.sh', '.gitignore', 'COMPATIBILITY.md'):
    path = root / name
    files.extend(path.rglob('*') if path.is_dir() else [path])
with tarfile.open(archive, 'w:gz') as tar:
    for path in sorted(files):
        if not path.is_file() or '__pycache__' in path.parts or path.suffix in ('.pyc', '.pyo'):
            continue
        if path.is_symlink(): raise ValueError('No symlinks in release')
        tar.add(path, arcname=f'arc-to-zen-{version}/' + str(path.relative_to(root)))
(out / 'SHA256SUMS').write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + '  ' + archive.name + '\n')
print(archive)
