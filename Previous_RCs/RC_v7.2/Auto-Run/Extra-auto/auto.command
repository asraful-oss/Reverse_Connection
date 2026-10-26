#!/bin/sh
# ============================================================================
# auto.sh — one-command bootstrap for auto.js (Linux, macOS, Android/Termux).
#
#   ./auto.sh  myprogram.py   --hidden
#   ./auto.sh  myprogram.js   --hidden
#   ./auto.sh  myprogram.cs   --hidden      # .py .js .ts .cs .sh all work
#
# A .js file cannot start without a JavaScript engine on the machine, so this
# tiny starter guarantees one: it uses the system Node if present, otherwise it
# quietly fetches an official portable Node into ~/.configs/runtime/node — in
# your home folder, with NO root / sudo / admin and no prompts — then runs
# auto.js, which makes every supported language "just run".
# ============================================================================
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AUTO_JS="$DIR/auto.js"

if [ ! -f "$AUTO_JS" ]; then
  echo "[-] auto.js not found next to auto.sh ($AUTO_JS)." >&2
  exit 1
fi

RT_DIR="$HOME/.configs/runtime/node"
RT_NODE="$RT_DIR/bin/node"

find_node() {
  for c in node nodejs; do
    if command -v "$c" >/dev/null 2>&1; then command -v "$c"; return 0; fi
  done
  if [ -x "$RT_NODE" ]; then echo "$RT_NODE"; return 0; fi
  return 1
}

is_termux() {
  case "${PREFIX:-}" in *com.termux*) return 0;; esac
  [ -n "${TERMUX_VERSION:-}" ]
}

download() { # url dest
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then wget -qO "$2" "$1"
  else echo "[-] Need curl or wget to fetch the Node runtime." >&2; return 1; fi
}

provision_node() {
  # Termux uses bionic libc; the nodejs.org glibc builds won't run there, so
  # use Termux's own package manager (still no root).
  if is_termux; then
    if command -v pkg >/dev/null 2>&1; then pkg install -y nodejs >/dev/null 2>&1 || pkg install -y nodejs-lts >/dev/null 2>&1 || true; fi
    command -v node >/dev/null 2>&1 && { command -v node; return 0; }
    return 1
  fi

  echo "[*] Setting up a private Node runtime (no admin needed)…" >&2
  os=$(uname -s); machine=$(uname -m)
  case "$os" in
    Linux)  plat=linux ;;
    Darwin) plat=darwin ;;
    *)      plat=linux ;;
  esac
  case "$machine" in
    x86_64|amd64)  arch=x64 ;;
    aarch64|arm64) arch=arm64 ;;
    armv7l|armv7)  arch=armv7l ;;
    ppc64le)       arch=ppc64le ;;
    s390x)         arch=s390x ;;
    *)             arch="$machine" ;;
  esac

  ver=""
  idx=$(mktemp) || return 1
  if download "https://nodejs.org/dist/index.json" "$idx" 2>/dev/null; then
    ver=$(tr '}' '\n' < "$idx" | grep -m1 '"lts":"[A-Za-z]' | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | head -1)
  fi
  rm -f "$idx"
  [ -z "$ver" ] && ver=v22.20.0   # safe fallback if the index is unreachable

  file="node-$ver-$plat-$arch.tar.gz"
  url="https://nodejs.org/dist/$ver/$file"
  mkdir -p "$RT_DIR"
  tmp=$(mktemp) || return 1
  if ! download "$url" "$tmp"; then rm -f "$tmp"; return 1; fi
  tar -xzf "$tmp" -C "$RT_DIR" --strip-components=1 || { rm -f "$tmp"; return 1; }
  rm -f "$tmp"
  [ -x "$RT_NODE" ] && { echo "$RT_NODE"; return 0; }
  return 1
}

NODE=$(find_node 2>/dev/null || true)
if [ -z "${NODE:-}" ]; then
  NODE=$(provision_node || true)
fi
if [ -z "${NODE:-}" ]; then
  echo "[-] Could not find or set up Node automatically." >&2
  exit 1
fi

exec "$NODE" "$AUTO_JS" "$@"
