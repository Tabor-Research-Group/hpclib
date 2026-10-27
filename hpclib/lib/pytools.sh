# uv and pixi on a cluster: whether they are installed, installing them, and where they keep Pythons and caches.
#
#   hpclib_pytools [--json]                  what is installed (path, version) and each directory setting
#   hpclib_pytools install uv|pixi           install (or update) one with its official installer, into the
#                                            place hpclib looks: ~/.local/bin/uv, ~/.pixi/bin/pixi
#   hpclib_pytools set VAR DIR|default       remember a directory for this cluster (VAR: one of HPCLIB_PYTOOLS_VARS)
#
# The directories (HPCLIB_PYTOOLS_VARS) are the tools' own variables:
#   UV_PYTHON_INSTALL_DIR   where uv puts the Pythons it downloads (default ~/.local/share/uv/python)
#   UV_CACHE_DIR            uv's package cache (default ~/.cache/uv)
#   PIXI_CACHE_DIR          pixi's package cache (default ~/.cache/rattler/cache)
# Home directories are often small: a scratch directory is the usual place for all three.
#
# Which value each has, first set wins:
#   1. the environment (your ~/.bashrc, when it exports one, in a job that sources it)
#   2. what `hpclib_pytools set` (or the console's cluster Settings) saved: $HPCTUNNELS_DATA_DIR/settings/pytools.sh
#   3. the tool's default
# Sourcing hpclib.sh exports the saved ones that the environment doesn't set (hpclib_pytools_env), so job
# scripts that source hpclib, the REST server's environment syncs and your own shells agree.
# --json prints one line, HPCLIB_PYTOOLS {...}. This file runs on its own too (bash pytools.sh ...), as the
# console runs it over your login.

HPCLIB_PYTOOLS_VARS="UV_PYTHON_INSTALL_DIR UV_CACHE_DIR PIXI_CACHE_DIR"
HPCLIB_UV_INSTALLER="${HPCLIB_UV_INSTALLER:-https://astral.sh/uv/install.sh}"
HPCLIB_PIXI_INSTALLER="${HPCLIB_PIXI_INSTALLER:-https://pixi.sh/install.sh}"

_hpclib_pytools_settings_file() {
  printf '%s/settings/pytools.sh' "${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}"
}

_hpclib_pytools_known() {  # _hpclib_pytools_known VAR: whether VAR is one of HPCLIB_PYTOOLS_VARS
  local v
  for v in $HPCLIB_PYTOOLS_VARS; do [ "$v" = "$1" ] && return 0; done
  return 1
}

_hpclib_pytools_saved() {  # _hpclib_pytools_saved VAR: what the settings file says (or nothing)
  local file
  file=$(_hpclib_pytools_settings_file)
  [ -f "$file" ] && (unset "$1"; . "$file" > /dev/null 2>&1; printf '%s' "${!1:-}")
}

# Export the saved directories the environment doesn't already set (hpclib.sh runs this when it is sourced).
hpclib_pytools_env() {
  local file v saved
  file=$(_hpclib_pytools_settings_file)
  [ -f "$file" ] || return 0
  for v in $HPCLIB_PYTOOLS_VARS; do
    [ -n "${!v:-}" ] && continue
    saved=$(_hpclib_pytools_saved "$v")
    if [ -n "$saved" ]; then
      export "$v=$saved"
      HPCLIB_PYTOOLS_EXPORTED="${HPCLIB_PYTOOLS_EXPORTED:-} $v"
    fi
  done
  return 0
}

_hpclib_pytools_set() {  # _hpclib_pytools_set VAR DIR|default
  local var="$1" value="$2" file tmp v current
  if ! _hpclib_pytools_known "$var"; then
    echo "hpclib_pytools set: VAR is one of $HPCLIB_PYTOOLS_VARS" >&2
    return 2
  fi
  case "$value" in
    default|'') value='' ;;
    *$'\n'*|*$'\r'*) echo "hpclib_pytools set: a directory is one line" >&2; return 2 ;;
    '~'|'~/'*) value="$HOME${value#\~}" ;;
    /*) ;;
    *) echo "hpclib_pytools set: $var must be an absolute path (or default)" >&2; return 2 ;;
  esac
  [ "${#value}" -le 1000 ] || { echo "hpclib_pytools set: at most 1000 characters" >&2; return 2; }
  if [ -n "$value" ]; then
    mkdir -p "$value" 2> /dev/null || { echo "hpclib_pytools set: can't create $value" >&2; return 1; }
  fi
  file=$(_hpclib_pytools_settings_file)
  mkdir -p "$(dirname "$file")" || return 1
  tmp=$(mktemp "$file.XXXXXX") || return 1
  {
    echo "# hpclib's uv and pixi directories (hpclib_pytools set, or the console's cluster Settings);"
    echo "# environment variables (e.g. from ~/.bashrc) win"
    for v in $HPCLIB_PYTOOLS_VARS; do
      if [ "$v" = "$var" ]; then current="$value"; else current=$(_hpclib_pytools_saved "$v"); fi
      if [ -n "$current" ]; then printf '%s=%q\n' "$v" "$current"; fi
    done
  } > "$tmp" && chmod 600 "$tmp" && mv -f "$tmp" "$file"
}

_hpclib_pytools_find() {  # _hpclib_pytools_find uv|pixi: the program's path, as the REST server's "auto" finds it
  local c
  c=$(command -v "$1" 2> /dev/null) && [ -n "$c" ] && { printf '%s' "$c"; return 0; }
  case "$1" in
    uv) for c in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do [ -x "$c" ] && { printf '%s' "$c"; return 0; }; done ;;
    pixi) c="$HOME/.pixi/bin/pixi"; [ -x "$c" ] && { printf '%s' "$c"; return 0; } ;;
  esac
  return 1
}

_hpclib_pytools_fetch() {  # _hpclib_pytools_fetch URL: the installer script on stdout
  if command -v curl > /dev/null 2>&1; then
    curl -LsSf --max-time 120 "$1"
  elif command -v wget > /dev/null 2>&1; then
    wget -qO- --timeout=120 "$1"
  else
    echo "hpclib_pytools install: needs curl or wget" >&2
    return 1
  fi
}

_hpclib_pytools_install() {  # _hpclib_pytools_install uv|pixi
  local tool="$1" script status
  hpclib_pytools_env
  case "$tool" in
    uv)
      script=$(_hpclib_pytools_fetch "$HPCLIB_UV_INSTALLER") || { echo "hpclib_pytools: couldn't download $HPCLIB_UV_INSTALLER" >&2; return 1; }
      # into ~/.local/bin, without touching your shell startup files
      printf '%s\n' "$script" | env UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh 2>&1
      status=$?
      ;;
    pixi)
      script=$(_hpclib_pytools_fetch "$HPCLIB_PIXI_INSTALLER") || { echo "hpclib_pytools: couldn't download $HPCLIB_PIXI_INSTALLER" >&2; return 1; }
      printf '%s\n' "$script" | env PIXI_HOME="$HOME/.pixi" PIXI_NO_PATH_UPDATE=1 bash 2>&1
      status=$?
      ;;
    *) echo "hpclib_pytools install uv|pixi" >&2; return 2 ;;
  esac
  [ "$status" = 0 ] || return "$status"
  _hpclib_pytools_find "$tool" > /dev/null || { echo "hpclib_pytools: the installer finished, but $tool isn't where hpclib looks" >&2; return 1; }
}

_hpclib_pytools_dir() {  # _hpclib_pytools_dir VAR PROGRAM: what the tool itself uses when VAR is unset
  case "$1" in
    UV_PYTHON_INSTALL_DIR) [ -n "$2" ] && "$2" python dir 2> /dev/null || printf '%s' "$HOME/.local/share/uv/python" ;;
    UV_CACHE_DIR) [ -n "$2" ] && "$2" cache dir 2> /dev/null || printf '%s' "$HOME/.cache/uv" ;;
    PIXI_CACHE_DIR) printf '%s' "${RATTLER_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/rattler/cache}" ;;
  esac
}

_hpclib_pytools_report() {  # NUL-separated records for --json
  local tool path version v saved env_value source value program
  for tool in uv pixi; do
    path=$(_hpclib_pytools_find "$tool") || path=''
    version=''
    [ -n "$path" ] && version=$(timeout 20 "$path" --version 2> /dev/null | head -n 1)
    printf 'tool\0%s\0%s\0%s\0' "$tool" "$path" "$version"
  done
  for v in $HPCLIB_PYTOOLS_VARS; do
    saved=$(_hpclib_pytools_saved "$v")
    env_value="${!v:-}"
    case " ${HPCLIB_PYTOOLS_EXPORTED:-} " in *" $v "*) env_value='' ;; esac     # hpclib's own export
    case "$v" in UV_*) program=$(_hpclib_pytools_find uv) ;; *) program='' ;; esac
    if [ -n "$env_value" ] && [ "$env_value" != "$saved" ]; then source=env; value="$env_value"
    elif [ -n "$saved" ]; then source=settings; value="$saved"
    else source=default; value=$(unset "$v"; _hpclib_pytools_dir "$v" "$program")
    fi
    printf 'var\0%s\0%s\0%s\0%s\0' "$v" "$value" "$source" "$saved"
  done
  # what your shell startup files (~/.bashrc) set, in a new interactive shell without hpclib's exports
  local unset_args=() line
  for v in $HPCLIB_PYTOOLS_VARS; do unset_args+=(-u "$v"); done
  while IFS= read -r -d '' line; do
    printf 'shell\0%s\0%s\0' "${line%%=*}" "${line#*=}"
  done < <(env "${unset_args[@]}" timeout 20 bash -ic \
             'for v in '"$HPCLIB_PYTOOLS_VARS"'; do printf "HPCLIB_PYTOOLS_SHELL %s=%s\0" "$v" "${!v:-}"; done' \
             < /dev/null 2> /dev/null | tr -d '\n' | grep -ao 'HPCLIB_PYTOOLS_SHELL [A-Z_]*=[^[:cntrl:]]*' \
           | sed 's/^HPCLIB_PYTOOLS_SHELL //' | tr '\n' '\0')
}

hpclib_pytools() {
  case "${1:-}" in
    set) shift; _hpclib_pytools_set "$@"; return ;;
    install) shift; _hpclib_pytools_install "$@"; return ;;
    -h|--help) sed -n '1,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; return 0 ;;
    ''|--json) ;;
    *) echo "usage: hpclib_pytools [--json] | install uv|pixi | set VAR DIR|default" >&2; return 2 ;;
  esac
  if [ "${1:-}" != --json ]; then
    _hpclib_pytools_report | tr '\0' '\n' | awk '
      $0 == "tool" { getline t; getline p; getline v; printf "%-5s %s\n", t, (p == "" ? "not found" : p " (" v ")"); next }
      $0 == "var"  { getline n; getline val; getline s; getline saved; printf "%-22s %s (%s)\n", n, val, s }'
    return 0
  fi
  _hpclib_pytools_report | python3 -c '
import json, socket, sys
f = sys.stdin.buffer.read().decode(errors="replace").split("\0")
out = {"host": socket.gethostname(), "settings_file": sys.argv[1], "tools": {}, "dirs": {}}
i = 0
while i < len(f) - 1:
    if f[i] == "tool":
        name, path, version = f[i + 1:i + 4]
        out["tools"][name] = {"path": path or None, "version": (version.split() + [None, None])[1] if version.strip() else None}
        i += 4
    elif f[i] == "shell":
        name, value = f[i + 1:i + 3]
        out.setdefault("shell", {})[name] = value or None
        i += 3
    elif f[i] == "var":
        name, value, source, saved = f[i + 1:i + 5]
        out["dirs"][name] = {"value": value or None, "source": source, "saved": saved or None}
        i += 5
    else:
        i += 1
print("HPCLIB_PYTOOLS " + json.dumps(out))
' "$(_hpclib_pytools_settings_file)"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  hpclib_pytools "$@"
else
  hpclib_pytools_env
fi
