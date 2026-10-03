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
# usual development-server ports.
PORT_RANGE = (20000, 32000)
NAME_RE = re.compile(r"[^A-Za-z0-9._@-]+")
LIST_KEYS = ("login", "work_dirs", "binds", "local_roots", "tunnel_args")
# Tunnel settings agent_tunnel reads (the console's settings page writes them):
#   auto_approve_templates  all (default) | new | review
#   tunnel_args             sbatch options for the tunnel's own job, e.g. --time=12:00:00
#   connection_hours        how long an ssh login is kept for reuse once idle (default 12)
APPROVE_MODES = ("all", "new", "review")
TUNNEL_ARG_RE = re.compile(r"--(time|mem|partition|account|qos|cpus-per-task|constraint)=[A-Za-z0-9:._,+-]{1,64}")


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


def port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def random_port(taken=()):
    """A random port in PORT_RANGE that no profile uses and that is free on this machine."""
    taken = set(taken)
    for p in all_profiles():
        taken.update(p.get(k) for k in ("port", "process_port"))
    rng = random.SystemRandom()
    for _ in range(200):
        port = rng.randint(*PORT_RANGE)
        if port not in taken and port_free(port):
            return port
    raise SystemExit("agent_profiles: could not find a free port")


def mcp_entry(profile, python, rest_mcp):
    args = [rest_mcp, "--url", f"http://127.0.0.1:{profile['port']}", "--token-file", profile["token_file"]]
    for d in profile.get("local_roots") or []:
        args += ["--local-root", d]
    return {"mcpServers": {profile["mcp_name"]: {"command": python, "args": args}}}


def mcp_name_for(host, taken):
    """hpclib-<first label of the host>, made unique among the other profiles."""
    host = host.rsplit("@", 1)[-1]
    label = NAME_RE.sub("-", host.split(".")[0]).strip("-").lower() or "cluster"
    name, n = f"hpclib-{label}", 2
    while name in taken:
        name, n = f"hpclib-{label}-{n}", n + 1
    return name


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
        if key == "auto_approve_templates" and value not in APPROVE_MODES:
            print(f"agent_profiles: auto_approve_templates is one of {', '.join(APPROVE_MODES)}", file=sys.stderr)
            return 2
        if key in LIST_KEYS:
            items = profile.get(key) or [] if append else []
            if value and value not in items:
                items.append(value)
            profile[key] = items
        elif key in ("port", "process_port"):
            profile[key] = int(value)
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


COMMANDS = {
    "name": cmd_name, "get": cmd_get, "init": cmd_init, "set": cmd_set, "new-ports": cmd_new_ports,
    "mcp": cmd_mcp, "mcp-entry": cmd_mcp_entry, "find": cmd_find, "env": cmd_env, "list": cmd_list, "port-free": cmd_port_free,
    "dir": lambda name: print(profile_dir(name)) or 0, "root": lambda: print(root()) or 0,
}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in COMMANDS:
        print(f"usage: agent_profiles.py {{{','.join(sorted(COMMANDS))}}} ...", file=sys.stderr)
        return 2
    return COMMANDS[argv[0]](*argv[1:]) or 0


if __name__ == "__main__":
    sys.exit(main())
