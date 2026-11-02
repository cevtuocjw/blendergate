#!/bin/sh
# Cevtuo-Blendergate installer — puts `blq` in ~/.local/bin (or $BLQ_BIN).
#
#   curl -fsSL https://raw.githubusercontent.com/cevtuocjw/blendergate/main/install.sh | sh
#   BLQ_BIN="$HOME/bin" sh install.sh
#
# macOS / Linux. Python 3.9+ is required; Blender is detected at runtime.
set -eu

REPO="cevtuocjw/blendergate"
BIN="${BLQ_BIN:-$HOME/.local/bin}"
# BLQ_URL can point at a mirror or a local file.
URL="${BLQ_URL:-https://raw.githubusercontent.com/$REPO/main/blq.py}"

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "python3 not found (install Python 3.9+ first)"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "Python 3.9+ required (found $(python3 -V 2>&1))"

mkdir -p "$BIN"
tmp="$(mktemp "${TMPDIR:-/tmp}/blq.XXXXXX")"
trap 'rm -f "$tmp"' EXIT HUP INT TERM

say "▸ downloading $URL"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$URL" -o "$tmp" || die "download failed"
elif command -v wget >/dev/null 2>&1; then
  wget -qO "$tmp" "$URL" || die "download failed"
else
  die "need curl or wget"
fi

head -n 1 "$tmp" | grep -q python || die "downloaded file does not look like blq.py"

install -m 755 "$tmp" "$BIN/blq"
say "✓ installed $BIN/blq"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *)
    say ""
    say "  $BIN is not in your PATH. Add this to ~/.zshrc or ~/.bashrc:"
    say "      export PATH=\"$BIN:\$PATH\""
    say ""
    ;;
esac

"$BIN/blq" --version || die "installed file cannot run — is python3 working?"
say ""
say "Next:  blq open    (status page)"
say "       blq render your-script.py -- --res 700"
