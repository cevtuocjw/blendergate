#!/bin/sh
# Cevtuo-Blendergate installer — puts `blq` in ~/.local/bin (or $BLQ_BIN).
#
#   curl -fsSL https://raw.githubusercontent.com/cevtuocjw/blendergate/main/install.sh | sh
#   BLQ_BIN="$HOME/bin" sh install.sh
#   BLQ_URL="file:///path/to/blq.py" sh install.sh
#
# Tries GitHub direct, then three public mirrors. macOS / Linux.
# Python 3.9+ is required; Blender is detected at runtime.
set -eu

REPO="cevtuocjw/blendergate"
BIN="${BLQ_BIN:-$HOME/.local/bin}"
RAW="https://raw.githubusercontent.com/$REPO/main/blq.py"

# BLQ_URL wins; otherwise try direct GitHub first and fall back to mirrors.
if [ -n "${BLQ_URL:-}" ]; then
  SOURCES="$BLQ_URL"
else
  SOURCES="
$RAW
https://ghproxy.net/https://raw.githubusercontent.com/$REPO/main/blq.py
https://gh-proxy.com/https://raw.githubusercontent.com/$REPO/main/blq.py
https://ghfast.top/https://raw.githubusercontent.com/$REPO/main/blq.py"
fi

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "python3 not found (install Python 3.9+ first)"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "Python 3.9+ required (found $(python3 -V 2>&1))"

mkdir -p "$BIN"
tmp="$(mktemp "${TMPDIR:-/tmp}/blq.XXXXXX")"
trap 'rm -f "$tmp"' EXIT HUP INT TERM

fetched=0
for url in $SOURCES; do
  say "▸ downloading $url"
  if command -v curl >/dev/null 2>&1; then
    if curl -fsSL --max-time 60 "$url" -o "$tmp"; then fetched=1; break; fi
  elif command -v wget >/dev/null 2>&1; then
    if wget -q -T 60 -O "$tmp" "$url"; then fetched=1; break; fi
  else
    die "need curl or wget"
  fi
  say "  ✗ failed — trying next source"
done
[ "$fetched" = 1 ] || die "every download source failed; check your network"
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
