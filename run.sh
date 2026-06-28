#!/usr/bin/env bash
# EVA sandbox wrapper - runs the organism inside the hardened Docker container.
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p data/runtime data/state data/workspace

cmd="${1:-help}"
shift || true

if [[ "$cmd" != "build" && "$cmd" != "help" && ! -f .env ]]; then
  echo "WARN: No .env file found. Copy .env.example to .env and set your LLM credentials." >&2
fi

eva() { docker compose run --rm eva "$@"; }

case "$cmd" in
  build)    docker compose build ;;
  status)   eva status ;;
  rollback) eva rollback ;;
  work)     eva work "$@" ;;
  improve)  eva improve "$@" ;;
  review)   eva review "$@" ;;
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
  evolve  [N] [flags]   Run N autonomous evolution rounds
  rollback              Roll back to the last good release
  shell                 Open a shell inside the container (debug)

Autonomous (no per-step approval; safe because Docker contains it):
  ./run.sh evolve 3 --yes --allow-shell

Everything the organism evolves is on the host under ./data/
EOF
    ;;
esac
