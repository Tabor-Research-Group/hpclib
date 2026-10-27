#!/usr/bin/env python3
"""
Per-cluster agent profiles for setup_agents and agent_tunnel (lib/tunnels.sh).

Each cluster you set up for agents gets a directory under
~/.config/hpclib/agents/ (or $HPCLIB_AGENTS_DIR), named after its address
(e.g. `maboyer@entropy.chem.tamu.edu`):

  profile.json   the login (ssh options and [user@]host), the tunnel's ports,
                 the agent's directories, token names and files, MCP name
  agent_token    the scoped token for agents on this machine (mode 600)
  owner_token    the owner (full-access) token for that cluster (mode 600)
  mcp.json       the MCP client entry for this cluster

The ports are picked at random when a profile is made, then kept, so that two
people (or two clusters) don't fight over the same port.

Standard library only; run as `python3 agent_profiles.py COMMAND ...`.
"""
import json
import os
import random
import re
import socket
import sys
import tempfile
import time

# Below the ephemeral ranges of Linux (32768+) and macOS (49152+), above the
# usual development-server ports. The console's settings can choose another range (port_range()).
PORT_RANGE = (20000, 32000)
MIN_PORT_SPAN = 50
NAME_RE = re.compile(r"[^A-Za-z0-9._@-]+")
LIST_KEYS = ("login", "work_dirs", "binds", "local_roots", "tunnel_args", "mcp_args")
# Tunnel settings agent_tunnel reads (the console's settings page writes them):
#   auto_approve_templates  all (default) | new | review
#   tunnel_args             sbatch options for the tunnel's own job, e.g. --time=12:00:00
#   connection_hours        how long an ssh login is kept for reuse once idle (default 12)
#   rest_on                 where the REST server runs: job (default; a SLURM job on a compute node) or login (on
#                           the login node, for sites that would rather it didn't hold a share of a compute node)
APPROVE_MODES = ("all", "new", "review")
REST_ON = ("job", "login")
TUNNEL_ARG_RE = re.compile(r"--(?:(time|mem|partition|account|qos|cpus-per-task|constraint)=[A-Za-z0-9:._,+-]{1,64}|nodelist=[A-Za-z0-9._,+\[\]-]{1,256})")


def root():
    return os.path.expanduser(os.environ.get("HPCLIB_AGENTS_DIR") or "~/.config/hpclib/agents")


def profile_dir(name):
    return os.path.join(root(), name)


def profile_path(name):
    return os.path.join(profile_dir(name), "profile.json")


def name_for(host):
    """A directory name for a [user@]host address."""
    name = NAME_RE.sub("_", host).strip("._") or "cluster"
    return name[:120]


def load(name):
    try:
        with open(profile_path(name)) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def all_profiles():
    out = []
    if os.path.isdir(root()):
        for name in sorted(os.listdir(root())):
            if name.startswith(".") or not os.path.isdir(os.path.join(root(), name)):
                continue          # .settings.json and the like
            profile = load(name)
            if profile is not None:
                out.append(profile)
    return out


def write_private(path, text):
    os.makedirs(root(), mode=0o700, exist_ok=True)
    os.chmod(root(), 0o700)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    os.chmod(os.path.dirname(path), 0o700)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp.")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def save(profile):
    profile["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    write_private(profile_path(profile["name"]), json.dumps(profile, indent=2) + "\n")


def removed_root():
    return os.path.join(root(), ".removed")


def remove(name):
    """Take a profile out of use: its folder (profile, tokens, mcp.json) moves to .removed/NAME-WHEN, private like
    the rest, so it can be put back by moving it again. Returns where it went (None: no such profile)."""
    if load(name) is None:
        return None
    os.makedirs(removed_root(), mode=0o700, exist_ok=True)
    os.chmod(removed_root(), 0o700)
    base = os.path.join(removed_root(), f"{name}-{time.strftime('%Y%m%d-%H%M%S')}")
    dest, n = base, 1
    while os.path.exists(dest):
        n += 1
        dest = f"{base}-{n}"
    os.rename(profile_dir(name), dest)
    return dest


def port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def settings_path():
    """Settings for every profile (the port range), beside them; a dot file, so never a profile's name."""
    return os.path.join(root(), ".settings.json")


def check_port_range(lo, hi):
    if (isinstance(lo, bool) or isinstance(hi, bool) or not isinstance(lo, int) or not isinstance(hi, int)
            or not 1024 <= lo < hi <= 65535 or hi - lo < MIN_PORT_SPAN):
        raise ValueError(f"the port range is two ports from 1024 to 65535, at least {MIN_PORT_SPAN} apart")
    return lo, hi


def port_range():
    """Where new ports are picked from: the saved range, else PORT_RANGE."""
    try:
        with open(settings_path()) as f:
            lo, hi = json.load(f)["port_range"]
        return check_port_range(lo, hi)
    except (OSError, ValueError, KeyError, TypeError):
        return PORT_RANGE


def set_port_range(lo, hi):
    """Save the range new ports come from (None, None: back to PORT_RANGE); existing ports are kept."""
    try:
        with open(settings_path()) as f:
            settings = json.load(f)
        if not isinstance(settings, dict):
            settings = {}
    except (OSError, ValueError):
        settings = {}
    if lo is None and hi is None:
        settings.pop("port_range", None)
    else:
        settings["port_range"] = list(check_port_range(lo, hi))
    write_private(settings_path(), json.dumps(settings, indent=2) + "\n")
    return port_range()


def all_ports():
    """Every local port the profiles hold: (profile, app or None, which, port)."""
    out = []
    for p in all_profiles():
        for which in ("port", "process_port"):
            if p.get(which):
                out.append((p, None, which, int(p[which])))
        for app, conf in sorted((p.get("apps") or {}).items()):
            for which in ("port", "process_port"):
                if isinstance(conf, dict) and conf.get(which):
                    out.append((p, app, which, int(conf[which])))
    return out


def random_port(taken=()):
    """A random port in port_range() that no profile (or app of one) uses and that is free on this machine."""
    taken = set(taken)
    taken.update(port for _, _, _, port in all_ports())
    rng = random.SystemRandom()
    for _ in range(200):
        port = rng.randint(*port_range())
        if port not in taken and port_free(port):
            return port
    raise SystemExit("agent_profiles: could not find a free port")


def launcher(profile, python):
    """(command, args) that start rest_mcp.py: the profile's launch command and arguments (setup_agents
    --mcp-command/--mcp-args, e.g. /usr/bin/arch with -arm64 /path/to/python), else `python`."""
    command = profile.get("mcp_command") or profile.get("mcp_python")      # (mcp_python: hpclib 1.4 to 1.8)
    if command:
        return command, list(profile.get("mcp_args") or []) if profile.get("mcp_command") else []
    return python, []


def find_mcp_python():
    """A Python here with the MCP SDK, as setup_agents finds one (_hpclib_mcp_python): $HPCLIB_MCP_PYTHON,
    install.sh's environment, then a python3 that has it (else plain python3)."""
    import shutil
    import subprocess
    if os.environ.get("HPCLIB_MCP_PYTHON"):
        return os.environ["HPCLIB_MCP_PYTHON"]
    venv = os.path.expanduser(os.environ.get("HPCLIB_MCP_VENV") or "~/.local/share/hpclib/mcp")
    for candidate in (os.path.join(venv, "bin", "python"), shutil.which("python3"), "/usr/local/bin/python3",
                      "/opt/homebrew/bin/python3"):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            try:
                if subprocess.run([candidate, "-c", "import mcp"], capture_output=True, timeout=20).returncode == 0:
                    return candidate
            except (OSError, subprocess.TimeoutExpired):
                continue
    return shutil.which("python3") or "python3"


def mcp_entry(profile, python, rest_mcp):
    command, before = launcher(profile, python)
    args = [*before, rest_mcp, "--url", f"http://127.0.0.1:{profile['port']}", "--token-file", profile["token_file"]]
    for d in profile.get("local_roots") or []:
        args += ["--local-root", d]
    return {"mcpServers": {profile["mcp_name"]: {"command": command, "args": args}}}


# An MCP server's name: what LLM clients call the cluster (a key in their configuration, a TOML bare key)
MCP_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
DEFAULT_MCP_PATTERN = "hpclib-{cluster}"
MCP_PATTERN_RE = re.compile(r"(?:[A-Za-z0-9_-]|\{(?:cluster|user|host)\}){1,64}")


def mcp_name_for(host, taken, pattern=None):
    """
    The pattern (default hpclib-{cluster}) for a login: {cluster} is the first label of its host, {host} the whole
    host with dots as dashes, {user} the login's user; made unique among the other profiles (-2, -3, ...).
    """
    user, _, address = host.rpartition("@")
    clean = lambda v: NAME_RE.sub("-", v).replace(".", "-").replace("@", "-").strip("-_").lower()
    values = {"cluster": clean(address.split(".")[0]) or "cluster", "host": clean(address) or "cluster",
              "user": clean(user) or "user"}
    pattern = pattern if pattern and MCP_PATTERN_RE.fullmatch(pattern) else DEFAULT_MCP_PATTERN
    base = re.sub(r"\{(cluster|user|host)\}", lambda m: values[m.group(1)], pattern)[:60].strip("-_") or "hpclib"
    if not MCP_NAME_RE.fullmatch(base):
        base = "hpclib-" + values["cluster"]
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def check_mcp_name(name, profile_name=None):
    """A valid MCP name no other profile has; ValueError otherwise."""
    if not isinstance(name, str) or not MCP_NAME_RE.fullmatch(name):
        raise ValueError("an MCP name is letters, digits, - and _ (at most 64), starting with a letter or digit")
    for p in all_profiles():
        if p["name"] != profile_name and p.get("mcp_name") == name:
            raise ValueError(f"{p['name']} is already called {name}")
    return name


def rename_mcp(profile, name):
    """Give the profile a new MCP name, keeping the old ones (previous_mcp_names: the console's to clean up)."""
    check_mcp_name(name, profile["name"])
    old = profile.get("mcp_name")
    if old and old != name:
        previous = [n for n in profile.get("previous_mcp_names") or [] if n != name]
        profile["previous_mcp_names"] = ([old] + [n for n in previous if n != old])[:20]
    profile["mcp_name"] = name
    return profile


################################################################################
##
##  Commands (called from bash)
##

def cmd_name(host):
    """Print the profile name for an address."""
    print(name_for(host))


def cmd_get(name, key=None):
    """Print one value (lists one item per line), or the whole profile as JSON."""
    profile = load(name)
    if profile is None:
        return 1
    if key is None:
        print(json.dumps(profile, indent=2))
        return 0
    value = profile.get(key)
    if value is None:
        return 1
    if isinstance(value, list):
        for v in value:
            print(v)
    else:
        print(value)
    return 0


def cmd_init(name, host):
    """Create the profile if it doesn't exist: random ports, default names and files."""
    profile = load(name)
    if profile is not None:
        print("existing")
        return 0
    taken = {p.get("mcp_name") for p in all_profiles()}
    port = random_port()
    d = profile_dir(name)
    machine = NAME_RE.sub("-", socket.gethostname().split(".")[0]).strip("-").lower() or "machine"
    profile = {
        "name": name,
        "host": host,
        "login": [],
        "port": port,
        "process_port": random_port(taken=[port]),
        "work_dirs": [],
        "binds": [],
        "local_roots": [],
        "token_name": f"agent-{machine}",
        "token_file": os.path.join(d, "agent_token"),
        "owner_token_file": os.path.join(d, "owner_token"),
        "mcp_name": mcp_name_for(host, taken),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    save(profile)
    print("created")
    return 0


def cmd_set(name, *pairs):
    """
    Set KEY=VALUE pairs. For list keys, KEY=VALUE replaces the list (KEY= empties
    it) and KEY+=VALUE appends to it.
    """
    profile = load(name)
    if profile is None:
        print(f"agent_profiles: no profile {name!r}", file=sys.stderr)
        return 1
    for pair in pairs:
        m = re.match(r"^([A-Za-z_]+)(\+?)=(.*)$", pair, re.S)
        if m is None:
            print(f"agent_profiles: expected KEY=VALUE, not {pair!r}", file=sys.stderr)
            return 2
        key, append, value = m.groups()
        if key == "tunnel_args" and value and not TUNNEL_ARG_RE.fullmatch(value):
            print(f"agent_profiles: tunnel_args takes sbatch options like --time=12:00:00, not {value!r}",
                  file=sys.stderr)
            return 2
        if key == "rest_on" and value not in REST_ON:
            print(f"agent_profiles: rest_on is one of {', '.join(REST_ON)}", file=sys.stderr)
            return 2
        if key == "auto_approve_templates" and value not in APPROVE_MODES:
            print(f"agent_profiles: auto_approve_templates is one of {', '.join(APPROVE_MODES)}", file=sys.stderr)
            return 2
        if key in LIST_KEYS:
            items = profile.get(key) or [] if append else []
            if value and (key == "mcp_args" or value not in items):     # (arguments may repeat)
                items.append(value)
            profile[key] = items
        elif key in ("port", "process_port"):
            profile[key] = int(value)
        elif key == "mcp_name":
            try:
                rename_mcp(profile, value)
            except ValueError as e:
                print(f"agent_profiles: {e}", file=sys.stderr)
                return 2
        elif key == "connection_hours":
            if not value.isdigit() or not 1 <= int(value) <= 168:
                print("agent_profiles: connection_hours is a whole number of hours from 1 to 168", file=sys.stderr)
                return 2
            profile[key] = int(value)
        else:
            profile[key] = value
    save(profile)
    return 0


def cmd_new_ports(name):
    profile = load(name)
    if profile is None:
        return 1
    profile["port"] = random_port(taken=[profile.get("port"), profile.get("process_port")])
    profile["process_port"] = random_port(taken=[profile["port"], profile.get("process_port")])
    save(profile)
    return 0


def cmd_mcp(name, python, rest_mcp):
    """Write mcp.json for the profile and print it."""
    profile = load(name)
    if profile is None:
        return 1
    text = json.dumps(mcp_entry(profile, python, rest_mcp), indent=2) + "\n"
    write_private(os.path.join(profile_dir(name), "mcp.json"), text)
    sys.stdout.write(text)
    return 0


def cmd_mcp_entry(name, python, rest_mcp):
    """The profile's server entry alone, on one line, for `claude mcp add-json`."""
    profile = load(name)
    if profile is None:
        return 1
    entry = dict(type="stdio", **mcp_entry(profile, python, rest_mcp)["mcpServers"][profile["mcp_name"]])
    print(json.dumps(entry))
    return 0


def cmd_find(address):
    """Print the name of the profile for a name, an address or an MCP name."""
    for p in all_profiles():
        if address in (p["name"], p.get("host"), p.get("mcp_name")):
            print(p["name"])
            return 0
    if load(name_for(address)) is not None:
        print(name_for(address))
        return 0
    return 1


def cmd_env(name):
    """`export` lines for scripts using RESTClient.from_env() (eval "$(agent_env NAME)")."""
    import shlex
    profile = load(name)
    if profile is None:
        return 1
    print(f"export HPC_REST_URL={shlex.quote('http://127.0.0.1:%d' % profile['port'])}")
    print(f"export HPC_REST_TOKEN_FILE={shlex.quote(profile['token_file'])}")
    return 0


def cmd_list():
    profiles = all_profiles()
    if not profiles:
        print(f"no agent profiles in {root()}; run setup_agents to make one")
        return 0
    print(f"{'NAME':<36} {'PORT':>5}  {'MCP NAME':<20} WORK DIRECTORIES")
    for p in profiles:
        print(f"{p['name']:<36} {p.get('port', ''):>5}  {p.get('mcp_name', ''):<20} {' '.join(p.get('work_dirs') or [])}")
    return 0


def cmd_port_free(port):
    return 0 if port_free(int(port)) else 1


def cmd_remove(name):
    """Move the profile to .removed/ and print where it went."""
    dest = remove(name)
    if dest is None:
        print(f"agent_profiles: no profile {name!r}", file=sys.stderr)
        return 1
    print(dest)
    return 0


COMMANDS = {
    "name": cmd_name, "get": cmd_get, "init": cmd_init, "set": cmd_set, "new-ports": cmd_new_ports,
    "mcp": cmd_mcp, "mcp-entry": cmd_mcp_entry, "find": cmd_find, "env": cmd_env, "list": cmd_list, "port-free": cmd_port_free,
    "dir": lambda name: print(profile_dir(name)) or 0, "root": lambda: print(root()) or 0,
    "remove": cmd_remove,
}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in COMMANDS:
        print(f"usage: agent_profiles.py {{{','.join(sorted(COMMANDS))}}} ...", file=sys.stderr)
        return 2
    return COMMANDS[argv[0]](*argv[1:]) or 0


if __name__ == "__main__":
    sys.exit(main())
