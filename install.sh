#!/bin/sh
# One-shot bootstrap. Only this invocation's private directory is removed.
set -eu
umask 077
VERSION=0.4.0
UV_VERSION=0.13.5
PYTHON_VERSION=3.12.12
REPOSITORY=https://github.com/luntikius/arc-to-zen
fail() { printf '%s\n' "arc-to-zen: $*" >&2; exit 1; }
[ "$(/usr/bin/uname -s)" = Darwin ] || fail 'macOS is required.'
[ "$(/usr/bin/uname -m)" = arm64 ] || fail 'Apple Silicon is required; run Terminal natively, without Rosetta.'
MAC_MAJOR=$(/usr/bin/sw_vers -productVersion | /usr/bin/cut -d. -f1)
[ "$MAC_MAJOR" -ge 11 ] || fail 'This runtime requires macOS 11 or newer.'
UV_SHA=743f85035a1996ecb350659e01cd9faea9709829be09b1b4b03c73eaf3c9cd73
SESSION=$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/arc-to-zen.XXXXXX")
: > "$SESSION/.arc-to-zen-owned"
cleanup() {
  if [ -d "$SESSION" ] && [ ! -L "$SESSION" ] && [ -f "$SESSION/.arc-to-zen-owned" ]; then
    if /bin/rm -rf "$SESSION"; then
      printf '%s\n' 'Temporary tool removed. Browser data and recovery backups were retained.'
    else
      printf 'Tool cleanup needs attention: %s\n' "$SESSION" >&2
      printf '%s\n' 'The migration result is unchanged. Recovery backups were retained.' >&2
    fi
  fi
}
trap 'install_status=$?; cleanup; exit "$install_status"' EXIT
CHILD=
exec 3<&0
run_owned() {
  "$@" <&3 &
  CHILD=$!
  child_status=0
  wait "$CHILD" || child_status=$?
  CHILD=
  return "$child_status"
}
interrupt() {
  trap '' INT TERM HUP
  if [ -n "$CHILD" ]; then
    kill -TERM "$CHILD" 2>/dev/null || :
    wait "$CHILD" 2>/dev/null || :
    CHILD=
  fi
  exit 130
}
trap interrupt INT TERM HUP
/bin/mkdir "$SESSION/tmp"
export TMPDIR="$SESSION/tmp"
export XDG_CACHE_HOME="$SESSION/cache"
fetch() { run_owned /usr/bin/curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fLsS --retry 3 "$1" -o "$2"; }
printf '%s\n' 'Preparing a private one-shot ArcToZen runtime…'
fetch "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-aarch64-apple-darwin.tar.gz" "$SESSION/uv.tar.gz"
printf '%s  %s\n' "$UV_SHA" "$SESSION/uv.tar.gz" | /usr/bin/shasum -a 256 -c - >/dev/null || fail 'uv checksum mismatch.'
run_owned /usr/bin/tar -xzf "$SESSION/uv.tar.gz" -C "$SESSION"
UV="$SESSION/uv-aarch64-apple-darwin/uv"
export UV_CACHE_DIR="$SESSION/cache"
export UV_PYTHON_INSTALL_DIR="$SESSION/python"
export UV_PYTHON_BIN_DIR="$SESSION/bin"
export UV_PYTHON_PREFERENCE=only-managed
export UV_NO_CONFIG=1
fetch "$REPOSITORY/releases/download/v$VERSION/arc-to-zen-$VERSION.tar.gz" "$SESSION/source.tar.gz"
fetch "$REPOSITORY/releases/download/v$VERSION/SHA256SUMS" "$SESSION/SHA256SUMS"
SOURCE_SHA=$(/usr/bin/awk -v name="arc-to-zen-$VERSION.tar.gz" '$2 == name {print $1}' "$SESSION/SHA256SUMS")
[ "${#SOURCE_SHA}" -eq 64 ] || fail 'Invalid release checksum manifest.'
printf '%s  %s\n' "$SOURCE_SHA" "$SESSION/source.tar.gz" | /usr/bin/shasum -a 256 -c - >/dev/null || fail 'Release checksum mismatch.'
/bin/mkdir "$SESSION/source"
run_owned "$UV" python install "$PYTHON_VERSION"
PYTHON=$("$UV" python find "$PYTHON_VERSION")
# Validate every member before extracting any release code. The bootstrap uv
# archive above is a fixed, independently pinned digest in this installer.
cat > "$SESSION/extract.py" <<'PYEXTRACT'
import os
import pathlib
import shutil
import sys
import tarfile

archive, destination, version = sys.argv[1:]
root = pathlib.Path(destination)
try:
    with tarfile.open(archive, "r:gz") as tar:
        members, seen, total = [], set(), 0
        for member in tar:
            parts = member.name.split("/")
            if (parts[0] != "arc-to-zen-" + version or len(parts) < 2 or
                    any(p in ("", ".", "..") for p in parts) or
                    not (member.isfile() or member.isdir()) or member.name in seen):
                raise ValueError()
            seen.add(member.name)
            total += member.size
            if len(seen) > 10000 or member.size < 0 or total > 100 * 1024 * 1024:
                raise ValueError()
            members.append(member)
        if not members:
            raise ValueError()
        for member in members:
            target = root.joinpath(*member.name.split("/")[1:])
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if member.isdir():
                target.mkdir(exist_ok=True, mode=0o700)
            else:
                with tar.extractfile(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o700 if member.mode & 0o111 else 0o600)
except (OSError, ValueError, tarfile.TarError):
    sys.exit("Invalid release archive; installation stopped.")
PYEXTRACT
run_owned "$PYTHON" "$SESSION/extract.py" "$SESSION/source.tar.gz" "$SESSION/source" "$VERSION"
run_owned "$UV" sync --project "$SESSION/source" --python "$PYTHON_VERSION" --frozen --no-dev --no-editable
if [ "$#" -eq 0 ]; then set -- wizard; fi
# Do not exec: the parent shell cleans up only after Python has exited.
run_owned "$SESSION/source/.venv/bin/python" -m arc_to_zen.cli "$@"
