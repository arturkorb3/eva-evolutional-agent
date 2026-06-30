#!/usr/bin/env bash
# EVA sandbox wrapper - runs the organism inside the hardened Docker container.
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p data/runtime data/state data/workspace data/local

# Bare `./run.sh` just starts EVA (work is the default mode), matching run.ps1.
cmd="${1:-work}"
shift || true

if [[ "$cmd" != "build" && "$cmd" != "help" && ! -f .env ]]; then
  echo "WARN: No .env file found. Copy .env.example to .env and set your LLM credentials." >&2
fi

eva() { docker compose run --rm eva "$@"; }

# Best-effort clipboard image grab (writes a PNG to $1; returns 0 on success).
eva_clip_grab() {
  local out="$1"
  if command -v pngpaste >/dev/null 2>&1; then
    pngpaste "$out" >/dev/null 2>&1 || true
    if [ -s "$out" ]; then return 0; fi
  fi
  if command -v wl-paste >/dev/null 2>&1; then
    wl-paste --type image/png > "$out" 2>/dev/null || true
    if [ -s "$out" ]; then return 0; fi
  fi
  if command -v xclip >/dev/null 2>&1; then
    xclip -selection clipboard -t image/png -o > "$out" 2>/dev/null || true
    if [ -s "$out" ]; then return 0; fi
  fi
  rm -f "$out" 2>/dev/null || true
  return 1
}

EVA_WATCHER_PID=""

# Auto-stage NEW clipboard screenshots into data/workspace/ while a session runs.
# Disable with EVA_NO_CLIP_WATCH=1. Silently skipped if no clipboard tool exists.
eva_start_watcher() {
  if [ "${EVA_NO_CLIP_WATCH:-}" = "1" ]; then return 0; fi
  if ! command -v pngpaste >/dev/null 2>&1 \
     && ! command -v wl-paste >/dev/null 2>&1 \
     && ! command -v xclip >/dev/null 2>&1; then
    return 0
  fi
  ( set +e
    last=""
    while true; do
      tmp="$(mktemp)"
      if eva_clip_grab "$tmp"; then
        h="$( (sha256sum "$tmp" 2>/dev/null || shasum -a 256 "$tmp" 2>/dev/null) | cut -d' ' -f1 )"
        if [ -n "$h" ] && [ "$h" != "$last" ]; then
          last="$h"
          mv "$tmp" "data/workspace/clip-$(date +%Y%m%d-%H%M%S).png"
        else
          rm -f "$tmp"
        fi
      else
        rm -f "$tmp"
      fi
      sleep 1
    done ) &
  EVA_WATCHER_PID=$!
  echo "(clipboard watch on: screenshots auto-stage to data/workspace; type /paste in chat to attach the latest)" >&2
}

eva_stop_watcher() {
  if [ -n "${EVA_WATCHER_PID:-}" ]; then kill "$EVA_WATCHER_PID" 2>/dev/null || true; fi
  EVA_WATCHER_PID=""
}

case "$cmd" in
  build)    docker compose build ;;
  status)   eva status ;;
  rollback) eva rollback ;;
  reseed)
    # Drop the materialized runtime so the next start re-seeds v001 from seed/
    # (mounted), without an image rebuild. State/workspace are kept.
    rm -rf data/runtime
    eva status
    ;;
  work)
    eva_start_watcher
    trap eva_stop_watcher EXIT INT TERM
    eva work "$@"
    ;;
  improve)
    eva_start_watcher
    trap eva_stop_watcher EXIT INT TERM
    eva improve "$@"
    ;;
  review)   eva review "$@" ;;
  paste)
    mkdir -p data/workspace
    name="clip-$(date +%Y%m%d-%H%M%S).png"
    if eva_clip_grab "data/workspace/$name"; then
      echo "Saved clipboard image -> data/workspace/$name"
      echo "Reference it in your next message, e.g.:  ![]($name)   or simply:  $name"
    else
      echo "No usable image in the clipboard (need pngpaste on macOS, or wl-paste/xclip on Linux)." >&2
    fi
    ;;
  evolve)
    rounds=1
    if [[ "${1:-}" =~ ^[0-9]+$ ]]; then rounds="$1"; shift; fi
    eva evolve --rounds "$rounds" "$@"
    ;;
  shell)    docker compose run --rm --entrypoint /bin/sh eva ;;
  *)
    cat <<'EOF'
EVA - Evolutional Agent (Docker sandbox)

Usage: ./run.sh <command> [args]

  build                 Build (or rebuild) the sandbox image
  status                Show current / last-good release
  work    [task]        Useful work in workspace/ (default mode)
  improve [task]        Evolve a candidate release
  review  [task]        Read-only inspection
  paste                 Save a clipboard screenshot into data/workspace/ (needs pngpaste/wl-paste/xclip)
  evolve  [N] [flags]   Run N autonomous evolution rounds
  rollback              Roll back to the last good release
  reseed                Re-seed v001 from seed/ (after editing the genome; no rebuild)
  shell                 Open a shell inside the container (debug)

Autonomous (no per-step approval; safe because Docker contains it):
  ./run.sh evolve 3 --yes --allow-shell

Everything the organism evolves is on the host under ./data/
EOF
    ;;
esac
