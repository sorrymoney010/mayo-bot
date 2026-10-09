#!/usr/bin/env bash
# Self-healing Python runtime for the mayo-bot PAPER loop on the box. No secrets.
#
# WHY: the box's file persistence drops directories named `.venv/`, `venv/`, `__pycache__/`,
# `.cache/`, `build/`, `dist/` ... when the box moves to a fresh instance (default
# workspace ignore list), and system packages are not guaranteed to survive either. On
# 2026-10-04 the repo .venv vanished that way and the bot stayed dead for 5 days.
#
# Layout (all under $MAYO_RUNTIME, default /workspace/mayo-runtime; names chosen so the
# box persistence keeps them):
#   bin/uv                      pinned copy of uv (falls back to /usr/local/bin/uv)
#   python/cpython-3.13.*/      uv-managed standalone CPython (independent of /usr/bin/python3)
#   archive/cpython-*.tar.gz    pristine tarball of that interpreter (offline restore)
#   wheelhouse/*.whl            every dependency wheel -> rebuild works offline
#   pyenv/                      the virtualenv the bot runs ($PY)
# The pinned dependency list is deploy/box/requirements.box.lock (in the repo).
#
# Sourced by box_env.sh (functions: venv_ok, ensure_venv). CLI:
#   box_venv.sh check     -> exit 0 if the runtime venv imports the bot
#   box_venv.sh ensure    -> verify; rebuild (locked) if broken; print the python path
#   box_venv.sh bootstrap -> (online) fetch standalone python, uv copy, wheelhouse; then ensure
: "${REPO:=/workspace/mayo-bot}"
MAYO_RUNTIME=${MAYO_RUNTIME:-/workspace/mayo-runtime}
MAYO_PY_VERSION=${MAYO_PY_VERSION:-3.13}
PYENV="$MAYO_RUNTIME/pyenv"
RUNTIME_PY="$PYENV/bin/python"
LEGACY_PY="$REPO/.venv/bin/python"
REQ_LOCK=${MAYO_REQ_LOCK:-$REPO/deploy/box/requirements.box.lock}
WHEELS="$MAYO_RUNTIME/wheelhouse"
VLOG=${MAYO_VENV_LOG:-$REPO/logs/box_venv_rebuild.log}
VENV_IMPORT_CHECK='import numpy, pandas, pydantic, pydantic_settings, requests, websockets
import dublin_bot.config, dublin_bot.engine, dublin_bot.instance, dublin_bot.loop_telemetry
import dublin_bot.exit_watcher, dublin_bot.meanrev_sleeve, dublin_bot.trendhold_sleeve
import dublin_bot.pipeline.collector, dublin_bot.pipeline.kraken_rest, dublin_bot.pipeline.tickstore
import dublin_bot.watchdog, dublin_bot.scorecard'

vlog() { mkdir -p "$(dirname "$VLOG")"; echo "$(date "+%F %T %Z") [pid $$] $*" >> "$VLOG"; }

# 0 = interpreter exists, runs, and imports every module the paper loop/ticks/health need.
venv_ok() {
  local py=${1:-$RUNTIME_PY}
  [[ -x "$py" ]] || return 1
  env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/home/box LANG=C.UTF-8 PYTHONPATH="$REPO/src" \
    PYTHONDONTWRITEBYTECODE=1 timeout 180 "$py" -c "$VENV_IMPORT_CHECK" >/dev/null 2>&1 9>&- 8>&- 7>&-
}

_uv() {
  local uv="$MAYO_RUNTIME/bin/uv"
  [[ -x "$uv" ]] && "$uv" --version >/dev/null 2>&1 || uv=$(command -v uv 2>/dev/null || true)
  [[ -n "$uv" ]] || return 127
  env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/home/box LANG=C.UTF-8 \
    UV_CACHE_DIR=/tmp/mayo-uv-cache UV_NO_CONFIG=1 UV_PYTHON_INSTALL_DIR="$MAYO_RUNTIME/python" \
    "$uv" "$@"
}

_py_ok() { [[ -x "$1" ]] && "$1" -c 'import ssl, sqlite3, ctypes, zlib, json' >/dev/null 2>&1; }

# Print a working base interpreter: standalone (dir) -> standalone (restored from tarball)
# -> standalone (downloaded, online) -> system python3 (last resort).
_base_python() {
  local p t
  for p in $(ls -d "$MAYO_RUNTIME"/python/cpython-"$MAYO_PY_VERSION".*-linux-*/bin/python"$MAYO_PY_VERSION" 2>/dev/null | sort -V -r); do
    _py_ok "$p" && { echo "$p"; return 0; }
  done
  t=$(ls "$MAYO_RUNTIME"/archive/cpython-"$MAYO_PY_VERSION".*.tar.gz 2>/dev/null | sort -V | tail -1)
  if [[ -n "$t" ]]; then
    vlog "standalone python missing/broken; restoring from $t"
    mkdir -p "$MAYO_RUNTIME/python"
    local name; name=$(basename "$t" .tar.gz)
    rm -rf "$MAYO_RUNTIME/python/$name.restore" && mkdir -p "$MAYO_RUNTIME/python/$name.restore" &&
      tar --no-same-owner -C "$MAYO_RUNTIME/python/$name.restore" -xzf "$t" 2>>"$VLOG" &&
      rm -rf "$MAYO_RUNTIME/python/$name" && mv "$MAYO_RUNTIME/python/$name.restore/$name" "$MAYO_RUNTIME/python/$name" &&
      rm -rf "$MAYO_RUNTIME/python/$name.restore"
    p="$MAYO_RUNTIME/python/$name/bin/python$MAYO_PY_VERSION"
    _py_ok "$p" && { echo "$p"; return 0; }
  fi
  vlog "trying online install of standalone python $MAYO_PY_VERSION via uv"
  if _uv python install "$MAYO_PY_VERSION" >> "$VLOG" 2>&1; then
    for p in $(ls -d "$MAYO_RUNTIME"/python/cpython-"$MAYO_PY_VERSION".*-linux-*/bin/python"$MAYO_PY_VERSION" 2>/dev/null | sort -V -r); do
      _py_ok "$p" && { _archive_python "$(dirname "$(dirname "$p")")"; echo "$p"; return 0; }
    done
  fi
  for p in /usr/bin/python"$MAYO_PY_VERSION" /usr/bin/python3; do
    _py_ok "$p" && { vlog "WARNING falling back to system interpreter $p"; echo "$p"; return 0; }
  done
  return 1
}

# Keep a pristine tarball of the standalone interpreter so it can be restored offline.
_archive_python() {
  local d=$1 name; name=$(basename "$d"); mkdir -p "$MAYO_RUNTIME/archive"
  [[ -f "$MAYO_RUNTIME/archive/$name.tar.gz" ]] && return 0
  tar -C "$(dirname "$d")" -czf "$MAYO_RUNTIME/archive/$name.tar.gz.tmp" "$name" 2>>"$VLOG" &&
    mv "$MAYO_RUNTIME/archive/$name.tar.gz.tmp" "$MAYO_RUNTIME/archive/$name.tar.gz" &&
    vlog "archived standalone python -> archive/$name.tar.gz"
}

# Build $PYENV from scratch (caller holds the lock). Offline wheelhouse first, then PyPI.
_rebuild_venv() {
  local base t0=$SECONDS
  base=$(_base_python) || { vlog "FAIL no usable base python"; return 1; }
  vlog "rebuild start base=$base req=$REQ_LOCK wheels=$(ls "$WHEELS"/*.whl 2>/dev/null | wc -l)"
  if [[ -e "$PYENV" ]]; then
    rm -rf "$MAYO_RUNTIME/pyenv.broken"; mv "$PYENV" "$MAYO_RUNTIME/pyenv.broken"
    vlog "moved broken env aside to pyenv.broken"
  fi
  local mode=""
  if _uv venv --python "$base" "$PYENV" >> "$VLOG" 2>&1; then
    if _uv pip install --python "$RUNTIME_PY" --no-index --find-links "$WHEELS" -r "$REQ_LOCK" >> "$VLOG" 2>&1; then
      mode="uv-offline"
    elif _uv pip install --python "$RUNTIME_PY" -r "$REQ_LOCK" >> "$VLOG" 2>&1; then
      mode="uv-online"
    fi
  fi
  if [[ -z "$mode" ]]; then   # no uv: stdlib venv + pip (system python has the venv module)
    rm -rf "$PYENV"
    local vb=$base; "$vb" -c 'import venv, ensurepip' 2>/dev/null || vb=/usr/bin/python3
    if "$vb" -m venv "$PYENV" >> "$VLOG" 2>&1; then
      if env -i PATH=/usr/bin:/bin HOME=/home/box "$RUNTIME_PY" -m pip install --no-index --find-links "$WHEELS" -r "$REQ_LOCK" >> "$VLOG" 2>&1; then
        mode="pip-offline"
      elif env -i PATH=/usr/bin:/bin HOME=/home/box "$RUNTIME_PY" -m pip install -r "$REQ_LOCK" >> "$VLOG" 2>&1; then
        mode="pip-online"
      fi
    fi
  fi
  if [[ -n "$mode" ]] && venv_ok "$RUNTIME_PY"; then
    vlog "rebuild OK mode=$mode in $((SECONDS - t0))s python=$("$RUNTIME_PY" -V 2>&1)"
    [[ "$mode" == *online ]] && _refresh_wheelhouse
    return 0
  fi
  vlog "rebuild FAILED (mode=${mode:-none}) after $((SECONDS - t0))s"
  return 1
}

# Online only: (re)populate the offline wheelhouse from the lock file.
_refresh_wheelhouse() {
  local tmp=/tmp/mayo-wheel-dl.$$ base
  base=$(_base_python) || return 1
  rm -rf "$tmp"; mkdir -p "$WHEELS"
  if _uv venv --seed --python "$base" "$tmp" >> "$VLOG" 2>&1 &&
     env -i PATH=/usr/bin:/bin HOME=/home/box "$tmp/bin/python" -m pip download --only-binary=:all: \
       -d "$WHEELS" -r "$REQ_LOCK" >> "$VLOG" 2>&1; then
    vlog "wheelhouse refreshed: $(ls "$WHEELS"/*.whl | wc -l) wheels"
  else
    vlog "WARNING wheelhouse refresh failed"
  fi
  rm -rf "$tmp"
}

# Sets PY to a verified interpreter. Rebuilds the runtime venv under a lock if needed.
# Fallback: the legacy repo .venv if it still works. Returns 1 if nothing works.
ensure_venv() {
  if venv_ok "$RUNTIME_PY"; then PY="$RUNTIME_PY"; return 0; fi
  mkdir -p "$MAYO_RUNTIME"
  vlog "runtime venv missing/broken ($RUNTIME_PY); acquiring rebuild lock (caller: ${1:-?})"
  (
    exec 7>>"$MAYO_RUNTIME/.rebuild.lock"
    if ! flock -w 1800 7; then vlog "timed out waiting for rebuild lock"; exit 1; fi
    if venv_ok "$RUNTIME_PY"; then vlog "another process already rebuilt it"; exit 0; fi
    _rebuild_venv
  ) 9>&- 8>&-
  if venv_ok "$RUNTIME_PY"; then PY="$RUNTIME_PY"; return 0; fi
  if venv_ok "$LEGACY_PY"; then
    vlog "using legacy $LEGACY_PY (runtime venv rebuild failed)"; PY="$LEGACY_PY"; return 0
  fi
  vlog "NO working python; caller will retry with backoff"
  return 1
}

# One-time / refresh (needs network): pin uv + standalone python + tarball + wheelhouse.
bootstrap_runtime() {
  mkdir -p "$MAYO_RUNTIME"/{bin,python,archive,wheelhouse}
  ( exec 7>>"$MAYO_RUNTIME/.rebuild.lock"; flock -w 1800 7 || exit 1
    local src; src=$(command -v uv) && { cp -f "$src" "$MAYO_RUNTIME/bin/uv.tmp" && mv -f "$MAYO_RUNTIME/bin/uv.tmp" "$MAYO_RUNTIME/bin/uv"; }
    _uv python install "$MAYO_PY_VERSION" >> "$VLOG" 2>&1 || { vlog "bootstrap: python install failed"; exit 1; }
    local d; d=$(ls -d "$MAYO_RUNTIME"/python/cpython-"$MAYO_PY_VERSION".*-linux-* 2>/dev/null | grep -v '\.restore$' | sort -V | tail -1)
    local name; name=$(basename "$d"); _archive_python "$d"
    _refresh_wheelhouse
    vlog "bootstrap done: python=$name wheels=$(ls "$WHEELS"/*.whl | wc -l)"
  ) 9>&- 8>&-
  ensure_venv bootstrap
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -u
  case "${1:-check}" in
    check) venv_ok "$RUNTIME_PY" && { echo "ok $RUNTIME_PY"; exit 0; } || { echo "BROKEN $RUNTIME_PY"; exit 1; } ;;
    ensure) ensure_venv cli && { echo "$PY"; exit 0; } || exit 1 ;;
    bootstrap) bootstrap_runtime && { echo "$PY"; exit 0; } || exit 1 ;;
    *) echo "usage: $0 check|ensure|bootstrap" >&2; exit 2 ;;
  esac
fi
