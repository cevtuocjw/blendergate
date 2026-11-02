#!/bin/sh
# Smoke test — no Blender required.
#   sh tests/smoke.sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BLQ="$ROOT/blq.py"
DIR="$(mktemp -d "${TMPDIR:-/tmp}/blq-smoke.XXXXXX")"
PORT="${BLQ_SMOKE_PORT:-8199}"
trap 'rm -rf "$DIR"; [ -n "${SPID:-}" ] && kill "$SPID" 2>/dev/null || true' EXIT HUP INT TERM

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

printf '▸ version\n'
"$BLQ" --version | grep -q '^blq ' || fail "--version"

printf '▸ gate runs a command\n'
export BLQ_STATE="$DIR/state" BLQ_NO_SERVE=1
"$BLQ" run --label smoke -- /usr/bin/true >/dev/null 2>&1 || fail "blq run exited non-zero"

printf '▸ status reports the queue\n'
"$BLQ" status >/dev/null || fail "blq status exited non-zero"

printf '▸ status page serves JSON and exits when idle\n'
unset BLQ_NO_SERVE
"$BLQ" serve --port "$PORT" --idle 5 >"$DIR/serve.log" 2>&1 &
SPID=$!
i=0
while [ $i -lt 30 ]; do
  curl -fsS "http://127.0.0.1:$PORT/status.json" >/dev/null 2>&1 && break
  i=$((i + 1)); sleep 0.2
done
[ $i -lt 30 ] || fail "status page never came up"
curl -fsS "http://127.0.0.1:$PORT/status.json" | grep -q '"slots"' || fail "status.json shape"
sleep 12
kill -0 "$SPID" 2>/dev/null && fail "status page did not exit after idle timeout"
SPID=""

printf '✓ all smoke tests passed\n'
