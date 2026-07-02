#!/usr/bin/env bash
# EVA sandbox wrapper - runs the organism inside the hardened Docker container.
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p data/runtime data/state data/workspace data/local

# Sandbox selection: SAFE (default) or FREE (--free) - the user decides the containment
# level. FREE layers docker-compose.free.yml (writable rootfs + root + apt) on top.
COMPOSE_FILES=(-f docker-compose.yml)
if [[ "${1:-}" == "--free" ]]; then
  COMPOSE_FILES+=(-f docker-compose.free.yml)
  FREE=1
  shift
  printf '\n  !! EVA FREE sandbox: writable rootfs + root + apt inside the container.\n' >&2
  printf '     Bigger blast radius (still contained to the container + ./data; ephemeral).\n' >&2
  printf '     Omit --free for the default hardened SAFE sandbox.\n\n' >&2
fi

# Bare `./run.sh` just starts EVA (work is the default mode), matching run.ps1.
cmd="${1:-work}"
shift || true

# First-run onboarding (interactive .env setup) runs just before dispatch, below.

eva() { docker compose "${COMPOSE_FILES[@]}" run --rm eva "$@"; }

# A FREE (root) session can leave root-owned files in ./data that the default non-root
# SAFE container then cannot write. Reset ownership via a one-shot root (free) container.
repair_data_permissions() {
  docker compose -f docker-compose.yml -f docker-compose.free.yml run --rm \
    --entrypoint chown eva -R 10001:10001 /eva/runtime /eva/state /eva/workspace /eva/.local >/dev/null
}

# On Linux the sandbox writes to ./data as an unprivileged user (uid 10001), but the
# host-created data dirs are owned by YOU - so the container's first mkdir dies with
# "Permission denied". Auto-repair ONCE (idempotent, no host sudo) so the first run just
# works instead of forcing the user to run `fix-perms` by hand. The check is cheap and
# self-limiting: only GNU `stat -c` (Linux) reports the owner, so on macOS/Docker Desktop
# (whose mounts remap ownership anyway) it no-ops; once ./data is owned by 10001 it skips.
ensure_data_writable() {
  command -v stat >/dev/null 2>&1 || return 0
  local owner
  owner="$(stat -c '%u' data/runtime 2>/dev/null || true)"
  [ -n "$owner" ] || return 0          # non-GNU stat (macOS) -> nothing to do
  [ "$owner" = "10001" ] && return 0    # already owned by the sandbox user
  echo "(first run on Linux: repairing ./data ownership for the sandbox user - one-time)" >&2
  repair_data_permissions || true
}

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

# --------------------------------------------------------------------------- #
# First-run onboarding: interactively create or complete .env (provider + creds).
# Runs host-side before the container starts. API keys are entered masked and
# written to the gitignored .env. Skip with EVA_NO_SETUP=1.
# --------------------------------------------------------------------------- #
read_env_val() {
  [ -f .env ] || { printf ''; return 0; }
  local line=""
  line="$(grep -E "^[[:space:]]*$1[[:space:]]*=" .env 2>/dev/null | tail -1 || true)"
  printf '%s' "${line#*=}"
}

set_env_var() {
  touch .env
  local tmp; tmp="$(mktemp)"
  grep -vE "^[[:space:]]*$1[[:space:]]*=" .env > "$tmp" 2>/dev/null || true
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  mv "$tmp" .env
}

is_placeholder() {
  case "$1" in
    ""|*replace-me*|sk-replace*|sk-ant-replace*) return 0 ;;
    *) return 1 ;;
  esac
}

read_secret() {
  local s=""
  read -rs -p "$1: " s </dev/tty || true
  echo >&2
  printf '%s' "$s"
}

eva_env_tip() {
  echo "Tip: .env.example documents every option (model timeout, context budget,"
  echo "     autonomous mode, streaming, prompt caching, TUI...). Edit .env any time,"
  echo "     or copy .env.example over it for the fully-commented template."
}

eva_onboarding() {
  [ "${EVA_NO_SETUP:-}" = "1" ] && return 0
  local fresh=0 did=0 provider ans choice m e k
  [ -f .env ] || fresh=1
  if [ "$fresh" = "1" ]; then
    echo
    echo "I don't see a .env config yet."
    printf "Shall I set up EVA with you now? [Y/n] "
    read -r ans </dev/tty || ans=""
    case "$ans" in n|N|no|NO) echo "Skipping setup - EVA may not reach a model without credentials." >&2; return 0 ;; esac
    echo "# EVA configuration (created by run.sh setup)" > .env
    did=1
  fi

  provider="$(read_env_val EVA_PROVIDER)"
  if [ "$fresh" = "1" ] || [ -z "$provider" ]; then
    echo
    echo "Which model provider should EVA use?"
    echo "  [1] OpenAI-compatible  (OpenAI / Azure / Ollama / LM Studio / vLLM / OpenRouter)"
    echo "  [2] Anthropic Claude   (Messages API: native tools + prompt caching)"
    echo "  [3] Offline 'fake'     (no API key; smoke tests / dry runs)"
    printf "Choose [1/2/3] (default 1): "
    read -r choice </dev/tty || choice=""
    case "$choice" in 2) provider="anthropic" ;; 3) provider="fake" ;; *) provider="openai_chat" ;; esac
    set_env_var EVA_PROVIDER "$provider"; did=1
  fi

  if [ "$provider" = "fake" ]; then
    [ "$did" = "1" ] && { echo "Provider 'fake' needs no credentials - you're set."; eva_env_tip; echo; }
    return 0
  fi

  if [ "$provider" = "anthropic" ]; then
    if is_placeholder "$(read_env_val EVA_MODEL)" && is_placeholder "$(read_env_val LLM_MODEL)"; then
      echo
      echo "Which Claude model?"
      echo "  [1] claude-opus-4-8    (frontier; long-running agents & coding)"
      echo "  [2] claude-opus-4-6    (frontier; long-running agents & coding)"
      echo "  [3] claude-sonnet-4-6  (best speed / intelligence balance)"
      echo "  [4] claude-haiku-4-5   (fastest, near-frontier)"
      echo "  [5] other              (type a custom model id)"
      printf "Choose [1-5] (default 3): "
      read -r mc </dev/tty || mc=""
      case "$mc" in
        1) m="claude-opus-4-8" ;;
        2) m="claude-opus-4-6" ;;
        4) m="claude-haiku-4-5" ;;
        5) printf "Model id: "; read -r m </dev/tty || m="" ;;
        *) m="claude-sonnet-4-6" ;;
      esac
      [ -n "$m" ] && { set_env_var EVA_MODEL "$m"; did=1; }
    fi
    if is_placeholder "$(read_env_val EVA_API_KEY)" && is_placeholder "$(read_env_val ANTHROPIC_API_KEY)"; then
      k="$(read_secret 'Anthropic API key (sk-ant-...) [hidden]')"
      [ -n "$k" ] && { set_env_var EVA_API_KEY "$k"; did=1; }
    fi
  elif [ "$provider" = "openai_chat" ]; then
    if is_placeholder "$(read_env_val EVA_ENDPOINT)" && is_placeholder "$(read_env_val LLM_ENDPOINT)"; then
      printf "Chat Completions endpoint (Enter for https://api.openai.com/v1/chat/completions): "
      read -r e </dev/tty || e=""; [ -z "$e" ] && e="https://api.openai.com/v1/chat/completions"
      set_env_var EVA_ENDPOINT "$e"; did=1
    fi
    if is_placeholder "$(read_env_val EVA_MODEL)" && is_placeholder "$(read_env_val LLM_MODEL)"; then
      printf "Model name (e.g. gpt-5.5, llama3.1): "
      read -r m </dev/tty || m=""; [ -n "$m" ] && { set_env_var EVA_MODEL "$m"; did=1; }
    fi
    if is_placeholder "$(read_env_val EVA_API_KEY)" && is_placeholder "$(read_env_val LLM_API_KEY)" && is_placeholder "$(read_env_val OPENAI_API_KEY)"; then
      k="$(read_secret "API key [hidden] (type 'ollama' for a local server)")"
      [ -n "$k" ] && { set_env_var EVA_API_KEY "$k"; did=1; }
    fi
  fi

  [ "$did" = "1" ] && { echo "Saved to .env - starting EVA..."; eva_env_tip; echo; }
  return 0
}

case "$cmd" in
  work|improve|review|evolve) eva_onboarding ;;
esac

# Auto-repair ./data ownership on Linux before any command that runs the sandbox
# (the container writes as uid 10001; host-owned dirs would fail with PermissionError).
case "$cmd" in
  work|improve|review|evolve|status|reseed|shell|rollback|unlock) ensure_data_writable ;;
esac

case "$cmd" in
  build)    docker compose build ;;
  install)
    # One-shot setup: symlink `eva` (bin/eva) into ~/.local/bin, wire PATH +
    # tab-completion into your shell rc, and optionally build the image. Undo: uninstall.
    dir="$(cd "$(dirname "$0")" && pwd)"
    bindir="$HOME/.local/bin"
    mkdir -p "$bindir"
    # Self-heal the executable bit (files created on Windows / checked out without +x):
    # both run.sh and the eva shim must be executable, or `./run.sh` / `eva` -> permission denied.
    chmod +x "$dir/run.sh" "$dir/bin/eva" "$dir/completions/eva.bash" 2>/dev/null || true
    ln -sf "$dir/bin/eva" "$bindir/eva"
    echo "shim       : linked $bindir/eva -> $dir/bin/eva"
    case "${SHELL:-}" in *zsh) rc="$HOME/.zshrc" ;; *) rc="$HOME/.bashrc" ;; esac
    touch "$rc"
    tmp="$(mktemp)"
    # Strip any previous eva block, then append a fresh one (PATH + completion).
    awk -v b="# >>> eva >>>" -v e="# <<< eva <<<" '$0==b{s=1} !s{print} $0==e{s=0}' "$rc" > "$tmp" 2>/dev/null || true
    {
      echo "# >>> eva >>>"
      echo "export PATH=\"\$HOME/.local/bin:\$PATH\""
      echo "[ -f \"$dir/completions/eva.bash\" ] && source \"$dir/completions/eva.bash\""
      echo "# <<< eva <<<"
    } >> "$tmp"
    mv "$tmp" "$rc"
    echo "shell      : PATH + tab-completion wired into $rc"
    # Offer to build the sandbox image if Docker is present and it isn't built yet.
    if command -v docker >/dev/null 2>&1; then
      if docker image inspect eva:latest >/dev/null 2>&1; then
        echo "image      : eva:latest present"
      elif [ "${EVA_NO_SETUP:-}" != "1" ]; then
        printf "Build the sandbox image now? (needs Docker running) [Y/n] "
        read -r ans </dev/tty || ans=""
        case "$ans" in n|N|no|NO) echo "Skipped - build later with:  eva build" ;; *) docker compose build ;; esac
      fi
    fi
    echo
    echo "Done. Open a NEW terminal (or run: source $rc) and try:  eva <Tab>"
    echo "Then just:  eva"
    ;;
  uninstall)
    bindir="$HOME/.local/bin"
    if [ -L "$bindir/eva" ]; then rm -f "$bindir/eva"; echo "Removed $bindir/eva"; fi
    case "${SHELL:-}" in *zsh) rc="$HOME/.zshrc" ;; *) rc="$HOME/.bashrc" ;; esac
    if [ -f "$rc" ]; then
      tmp="$(mktemp)"
      awk -v b="# >>> eva >>>" -v e="# <<< eva <<<" '$0==b{s=1} !s{print} $0==e{s=0}' "$rc" > "$tmp" 2>/dev/null || true
      mv "$tmp" "$rc"
      echo "Removed eva block from $rc (open a new terminal)."
    fi
    ;;
  status)   eva status ;;
  rollback) eva rollback "$@" ;;
  unlock)   eva unlock ;;
  fix-perms)
    echo "Resetting ./data ownership to the sandbox user (10001) - undoes a prior free-mode run..."
    repair_data_permissions
    # On Linux the container writes as uid 10001, so the host user can't delete/edit
    # ./data afterwards. If ACLs are available, also grant the current host user rwx
    # (default ACL covers files created later) so reseed/edits work without sudo.
    # Needs sudo: after the chown above the files are owned by 10001, so the host
    # user can no longer set ACLs on them itself.
    if command -v setfacl >/dev/null 2>&1; then
      sudo setfacl -R -m "u:$(id -u):rwx,u:10001:rwx" data || true
      sudo setfacl -R -d -m "u:$(id -u):rwx,u:10001:rwx" data || true
    else
      echo "Tip: for hassle-free host access to ./data, install ACL support: sudo apt install acl"
    fi
    echo "Done."
    ;;
  reseed)
    # Drop the materialized runtime so the next start re-seeds v001 from seed/
    # (mounted), without an image rebuild. State/workspace are kept.
    # Clear it from INSIDE a one-shot container: the runtime is owned by the
    # sandbox user (uid 10001), so a host-side `rm -rf` fails on Linux with
    # "Permission denied". Running rm in the container sidesteps that entirely.
    echo "Clearing runtime from inside the container (avoids host/container permission issues)..."
    docker compose -f docker-compose.yml -f docker-compose.free.yml run --rm \
      --entrypoint /bin/sh eva -c 'find /eva/runtime -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +'
    repair_data_permissions
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
  shell)    docker compose "${COMPOSE_FILES[@]}" run --rm --entrypoint /bin/sh eva ;;
  *)
    cat <<'EOF'
EVA - Evolvable Virtual Agent (Docker sandbox)

Usage: ./run.sh <command> [args]   (or, after `install`, simply:  eva <command> [args])

  build                 Build (or rebuild) the sandbox image
  install               Set up the `eva` command: PATH + tab-completion (+ optional build)
  uninstall             Undo `install` (remove symlink + shell rc block)
  status                Show current / last-good release
  work    [task]        Useful work in workspace/ (default mode)
  improve [task]        Evolve a candidate release
  review  [task]        Read-only inspection
  paste                 Save a clipboard screenshot into data/workspace/ (needs pngpaste/wl-paste/xclip)
  evolve  [N] [flags]   Run N autonomous evolution rounds
  rollback [--force]    Roll back to the last good release
  unlock                Clear a dead evolution lock (single-writer guard)
  reseed                Re-seed v001 from seed/ (after editing the genome; no rebuild)
  shell                 Open a shell inside the container (debug)
  fix-perms             Reset ./data ownership to the sandbox user (after a --free run)

Sandbox mode:
  --free <command>      Run in the FREE sandbox (writable rootfs + root + apt) instead of
                        the default hardened SAFE sandbox. Lets EVA install system packages
                        (e.g. browser libs). Bigger blast radius - use only when needed.
                        e.g.  ./run.sh --free work

Autonomous (no per-step approval; safe because Docker contains it):
  ./run.sh evolve 3 --yes --allow-shell

Everything the organism evolves is on the host under ./data/
EOF
    ;;
esac

# A FREE (root) session can leave root-owned files in ./data that break the next SAFE run;
# reset ownership afterwards so safe mode keeps working.
if [[ "${FREE:-}" == 1 ]] && [[ "$cmd" =~ ^(work|improve|review|evolve|shell)$ ]]; then
  repair_data_permissions
fi
