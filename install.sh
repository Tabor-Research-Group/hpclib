#!/usr/bin/env bash
# hpclib's installer for your machine (macOS, Linux, or Linux in WSL on Windows):
#
#   curl -fsSL https://raw.githubusercontent.com/Tabor-Research-Group/hpclib/master/install.sh | bash
#   wget -qO- https://raw.githubusercontent.com/Tabor-Research-Group/hpclib/master/install.sh | bash
#
# Options go after `bash -s --`, e.g. `... | bash -s -- --dir ~/src/hpclib --no-mcp`:
#
#   --dir DIR        where the clone goes (default ~/hpclib, or $HPCLIB_INSTALL_DIR)
#   --branch NAME    the branch to clone (default: the repository's default)
#   --repo URL       another repository (a fork)
#   --bin DIR        where the launch-tunnel-manager link goes (default ~/.local/bin)
#   --no-mcp         skip the MCP SDK (only agents need it; setup_agents says how to add it later)
#   --no-shell       leave your shell's startup files alone
#   --dry-run        say what would happen, change nothing
#
# It clones hpclib (or updates an earlier clone with `git pull --ff-only`), links the tunnel manager's launcher
# (launch-tunnel-manager) onto your PATH, loads hpclib's shell functions in new bash shells, and installs the MCP
# SDK for the agents' MCP server into a virtual environment of its own (~/.local/share/hpclib/mcp), which
# setup_agents finds. Running it again updates. Nothing needs root. Windows: install.ps1 runs this inside WSL.
#
# Everything is in functions, called on the last line, so a download cut short runs nothing.

hpclib_install_main() {
  set -u
  local repo="${HPCLIB_INSTALL_REPO:-https://github.com/Tabor-Research-Group/hpclib.git}"
  local dir="${HPCLIB_INSTALL_DIR:-$HOME/hpclib}" branch="" bin="$HOME/.local/bin" mcp=yes shell=yes dry=no
  local mcp_dir="${HPCLIB_MCP_VENV:-$HOME/.local/share/hpclib/mcp}"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dir) dir="$2"; shift 2 ;;
      --branch) branch="$2"; shift 2 ;;
      --repo) repo="$2"; shift 2 ;;
      --bin) bin="$2"; shift 2 ;;
      --no-mcp) mcp=no; shift ;;
      --no-shell) shell=no; shift ;;
      --dry-run) dry=yes; shift ;;
      -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]:-/dev/null}" 2> /dev/null | sed 's/^# \{0,1\}//'
                 [ -f "${BASH_SOURCE[0]:-}" ] || echo "see the top of install.sh in the hpclib repository"
                 return 0 ;;
      *) _hi_fail "unknown option $1 (see --help)" ; return 2 ;;
    esac
  done
  case "$dir" in "~/"*) dir="$HOME/${dir#\~/}" ;; esac
  case "$bin" in "~/"*) bin="$HOME/${bin#\~/}" ;; esac

  _hi_say "hpclib installer ($(_hi_platform))"
  _hi_requirements || return 1

  # 1. the clone
  if [ -d "$dir/.git" ]; then
    _hi_step "updating $dir"
    if [ -n "$(git -C "$dir" status --porcelain --untracked-files=no 2> /dev/null)" ]; then
      _hi_warn "$dir has local changes: left as it is (commit or stash them, then run this again to update)"
    else
      _hi_run git -C "$dir" pull --ff-only --quiet || _hi_warn "git pull failed; keeping the version you have"
    fi
  elif [ -e "$dir" ] && [ -n "$(ls -A "$dir" 2> /dev/null)" ]; then
    _hi_fail "$dir exists and isn't a git clone of hpclib; choose another place with --dir"
    return 1
  else
    _hi_step "cloning $repo into $dir"
    _hi_run git clone --quiet ${branch:+--branch "$branch"} "$repo" "$dir" || { _hi_fail "git clone failed"; return 1; }
  fi
  local launcher="$dir/hpclib/launch-tunnel-manager"
  if [ "$dry" = no ] && [ ! -f "$launcher" ]; then
    _hi_fail "$launcher is missing: is $dir an hpclib clone?"
    return 1
  fi

  # 2. the launcher on PATH
  _hi_step "linking launch-tunnel-manager into $bin"
  _hi_run mkdir -p "$bin" && _hi_run ln -sf "$launcher" "$bin/launch-tunnel-manager" ||
    { _hi_fail "can't write to $bin; choose another place with --bin"; return 1; }

  # 3. new shells: hpclib's functions (bash) and the launcher's folder on PATH
  if [ "$shell" = yes ]; then
    _hi_rc "$HOME/.bashrc" "source \"$(_hi_home_rel "$dir")/hpclib/hpclib.sh\"" "$bin"
    case "${SHELL:-}" in
      */zsh) _hi_rc "$HOME/.zshrc" "" "$bin" ;;
    esac
    # macOS Terminal starts login shells, which read ~/.bash_profile, not ~/.bashrc
    if [ "$(uname -s)" = Darwin ] && [ -f "$HOME/.bash_profile" ] && ! grep -q '\.bashrc' "$HOME/.bash_profile"; then
      _hi_append "$HOME/.bash_profile" '[ -f ~/.bashrc ] && . ~/.bashrc'
    fi
  fi

  # 4. the MCP SDK, for agents
  if [ "$mcp" = yes ]; then
    _hi_mcp "$mcp_dir" || _hi_warn "the MCP SDK isn't installed; agents need it (rerun with Python 3.10 or newer, " \
      "or: python3 -m pip install mcp). The tunnel manager works without it"
  fi

  _hi_done "$dir" "$bin"
}

# ---------------------------------------------------------------- pieces

_hi_color() { [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; }
_hi_say() { if _hi_color; then printf '\033[1m%s\033[0m\n' "$*"; else printf '%s\n' "$*"; fi; }
_hi_step() { printf '  - %s\n' "$*"; }
_hi_warn() { if _hi_color; then printf '  \033[33m! %s\033[0m\n' "$*"; else printf '  ! %s\n' "$*"; fi; }
_hi_fail() { if _hi_color; then printf '\033[31mhpclib installer: %s\033[0m\n' "$*" >&2; else
             printf 'hpclib installer: %s\n' "$*" >&2; fi; }
_hi_run() {
  if [ "${dry:-no}" = yes ]; then printf '    would run: %s\n' "$*"; return 0; fi
  "$@"
}

_hi_platform() {
  case "$(uname -s)" in
    Darwin) echo macOS ;;
    Linux) if [ -n "${WSL_DISTRO_NAME:-}" ] || grep -qi microsoft /proc/sys/kernel/osrelease 2> /dev/null; then
             echo "WSL${WSL_DISTRO_NAME:+: $WSL_DISTRO_NAME}"; else echo Linux; fi ;;
    *) uname -s ;;
  esac
}

_hi_requirements() {
  local missing=() py
  command -v git > /dev/null 2>&1 || missing+=(git)
  command -v ssh > /dev/null 2>&1 || missing+=("ssh (OpenSSH)")
  py=$(command -v python3 2> /dev/null)
  if [ -z "$py" ] || ! "$py" -c 'import sys; sys.exit(sys.version_info < (3, 7))' 2> /dev/null; then
    missing+=("python3 (3.7 or newer)")
  fi
  if [ "${#missing[@]}" -gt 0 ]; then
    _hi_fail "first install: ${missing[*]}"
    if command -v apt-get > /dev/null 2>&1; then
      echo "  e.g. sudo apt-get install git openssh-client python3 python3-venv" >&2
    elif command -v dnf > /dev/null 2>&1; then
      echo "  e.g. sudo dnf install git openssh-clients python3" >&2
    elif [ "$(uname -s)" = Darwin ]; then
      echo "  e.g. xcode-select --install (git), and python3 from python.org or Homebrew" >&2
    fi
    return 1
  fi
}

_hi_home_rel() {  # a path under your home as $HOME/..., so the startup files keep working if it moves
  case "$1" in "$HOME"/*) printf '$HOME/%s' "${1#"$HOME"/}" ;; *) printf '%s' "$1" ;; esac
}

_hi_append() {  # FILE LINE: once
  local file="$1" line="$2"
  grep -qxF "$line" "$file" 2> /dev/null && return 0
  _hi_step "adding to $file: $line"
  [ "${dry:-no}" = yes ] && return 0
  printf '\n%s\n' "$line" >> "$file"
}

_hi_rc() {  # RC_FILE SOURCE_LINE BIN: hpclib's block in a shell's startup file, replaced on reruns
  local file="$1" source_line="$2" bin_dir tmp
  bin_dir=$(_hi_home_rel "$3")
  local begin="# >>> hpclib (install.sh) >>>" end="# <<< hpclib (install.sh) <<<"
  local block="$begin"$'\n'"case \":\$PATH:\" in *\":$bin_dir:\"*) ;; *) export PATH=\"$bin_dir:\$PATH\" ;; esac"
  [ -n "$source_line" ] && block+=$'\n'"$source_line"
  block+=$'\n'"$end"
  if [ -f "$file" ] && grep -qF "$begin" "$file"; then
    _hi_step "updating hpclib's lines in $file"
    [ "${dry:-no}" = yes ] && return 0
    tmp=$(mktemp "$file.hpclib.XXXXXX") || return 1
    HI_BLOCK="$block" awk -v b="$begin" -v e="$end" '
      $0 == b { print ENVIRON["HI_BLOCK"]; skip = 1; next }
      $0 == e { skip = 0; next }
      !skip { print }' "$file" > "$tmp" && cat "$tmp" > "$file"
    rm -f "$tmp"
  else
    _hi_step "adding hpclib's lines to $file"
    [ "${dry:-no}" = yes ] && return 0
    printf '\n%s\n' "$block" >> "$file"
  fi
}

_hi_mcp() {  # VENV_DIR: the MCP SDK in a virtual environment (it needs Python 3.10 or newer)
  local venv="$1" py candidate
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    py=$(command -v "$candidate" 2> /dev/null) || continue
    "$py" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2> /dev/null && break
    py=""
  done
  [ -n "$py" ] || return 1
  if [ -x "$venv/bin/python" ] && "$venv/bin/python" -c 'import mcp' 2> /dev/null; then
    _hi_step "MCP SDK: already in $venv"
    return 0
  fi
  _hi_step "MCP SDK: installing into $venv (with $py)"
  [ "${dry:-no}" = yes ] && return 0
  if ! "$py" -m venv "$venv" > /dev/null 2>&1; then
    rm -rf "$venv"
    _hi_warn "$py can't make a virtual environment (Debian and Ubuntu: sudo apt-get install python3-venv)"
    return 1
  fi
  "$venv/bin/python" -m pip install --quiet --upgrade pip > /dev/null 2>&1
  "$venv/bin/python" -m pip install --quiet mcp || return 1
  "$venv/bin/python" -c 'import mcp' 2> /dev/null
}

_hi_done() {
  local dir="$1" bin_dir="$2"
  echo
  _hi_say "hpclib is installed in $dir"
  if [ "${dry:-no}" = yes ]; then echo "(a dry run: nothing was changed)"; return 0; fi
  echo "  Start the tunnel manager (it opens in your browser):"
  echo "    launch-tunnel-manager"
  case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) echo "  (in this terminal, until you open a new one: $bin_dir/launch-tunnel-manager)" ;;
  esac
  echo "  In new bash shells, hpclib's commands (launch_tunnel, setup_agents, ...) are loaded."
  echo "  Update later by running this installer again, or: git -C $dir pull"
}

hpclib_install_main "$@"
