"""
The docs' screenshots of the console (docs/images), taken with fixture API answers: no cluster, login or
token is involved, and the names and numbers are made up.

  python3 docs/images/screenshots.py                  (from the repository's top folder)
  ONLY=about,menu python3 docs/images/screenshots.py  (retakes the pictures whose names start so)

It needs Playwright with Chromium (pip install playwright; playwright install chromium) and Pillow, which
reduces each picture to 256 colours. DOCS below lists each picture: its page, width,
colour scheme, whether packaged widgets show, what to click first, and what to keep (an element, or a region).
"""
import json
import re
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "hpclib" / "servers"))
os.environ["HPCLIB_CONSOLE_DIR"] = tempfile.mkdtemp()
import agent_console  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

NOW = time.time()
LOCAL = "1.4.0"
SHOW_WIDGETS = False


def cluster(name, host, login, tunnel, version=None, set_up=True, work=None):
    return {"name": name, "host": host, "port": 20735, "process_port": 23129, "work_dirs": work or [],
            "token_name": "llm", "mcp_name": name, "set_up": set_up, "has_owner_token": True, "has_agent_token": True,
            "login": {"state": "connected" if login else "none", "connection_hours": 12},
            "tunnel": {"state": tunnel, "port": 20735, "hpclib_version": version, "hostname": "g042",
                       "started_here": tunnel == "up"},
            "operation": None}


CLUSTERS = {
    "hpclib-grace": cluster("hpclib-grace", "jdoe@grace.hprc.tamu.edu", True, "up", "1.3.1",
                            work=["/scratch/user/jdoe/llm"]),
    "hpclib-faster": cluster("hpclib-faster", "jdoe@faster.hprc.tamu.edu", True, "down", None,
                             work=["/scratch/user/jdoe/llm"]),
    "hpclib-entropy": cluster("hpclib-entropy", "jdoe@entropy.chem.tamu.edu", False, "down", None,
                              work=["/home/jdoe/llm"]),
}

JOBS = {
    "hpclib-grace": {
        "queued": [
            {"id": "18842211", "name": "hpcrest-jax_bench", "state": "PENDING", "reason": "AssocGrpGRES", "partition": "gpu",
             "elapsed": "0:00", "time_limit": "2:00:00", "nodes": "1", "cpus": "8", "gres": "gpu:a100:1", "memory": "64G",
             "node_list": "", "start": "2026-10-23T14:40:00", "source": {"kind": "agent", "label": "agent: jax_bench", "token": "llm"}},
            {"id": "18842390_[0-47%8]", "name": "hpcrest-xtb_opt", "state": "PENDING", "reason": "Priority", "partition": "cpu",
             "elapsed": "0:00", "time_limit": "1:00:00", "nodes": "1", "cpus": "4", "gres": "", "memory": "8G", "node_list": "",
             "start": "", "source": {"kind": "agent", "label": "agent: xtb_opt", "token": "llm"}},
        ],
        "running": [
            {"id": "18841977", "name": "rest-k2v9x", "state": "RUNNING", "reason": "None", "partition": "cpu", "elapsed": "3:12:40",
             "time_limit": "8:00:00", "nodes": "1", "cpus": "1", "gres": "", "memory": "1G", "node_list": "g042", "start": "",
             "source": {"kind": "agent-tunnel", "label": "agent tunnel"}},
            {"id": "18841690", "name": "pai-7fq2m", "state": "RUNNING", "reason": "None", "partition": "cpu", "elapsed": "1-02:14:05",
             "time_limit": "7-00:00:00", "nodes": "1", "cpus": "4", "gres": "", "memory": "16G", "node_list": "g117", "start": "",
             "source": {"kind": "shared", "label": "PAI (database)", "app": "pai"}},
            {"id": "18842004", "name": "hpcrest-crest_conformers", "state": "RUNNING", "reason": "None", "partition": "cpu",
             "elapsed": "0:41:18", "time_limit": "4:00:00", "nodes": "1", "cpus": "16", "gres": "", "memory": "32G",
             "node_list": "g203", "start": "", "source": {"kind": "agent", "label": "agent: crest_conformers", "token": "llm"}},
            {"id": "18842120", "name": "orca_ts_scan", "state": "RUNNING", "reason": "None", "partition": "cpu", "elapsed": "0:12:02",
             "time_limit": "12:00:00", "nodes": "1", "cpus": "24", "gres": "", "memory": "96G", "node_list": "g088", "start": "",
             "source": {"kind": "other", "label": "other"}},
        ],
        "finished": [
            {"id": "18841901", "name": "hpcrest-xtb_opt", "state": "COMPLETED", "exit_code": "0:0", "elapsed": "00:18:22",
             "end": "2026-10-23T11:05:12", "partition": "cpu", "requested_memory": "8G", "peak_memory_mb": 2310.4,
             "source": {"kind": "agent", "label": "agent: xtb_opt", "token": "llm"}},
            {"id": "18841850", "name": "hpcrest-jax_bench", "state": "FAILED", "exit_code": "1:0", "elapsed": "00:02:41",
             "end": "2026-10-23T10:31:57", "partition": "gpu", "requested_memory": "64G", "peak_memory_mb": 5120.0,
             "source": {"kind": "agent", "label": "agent: jax_bench", "token": "llm"}},
            {"id": "18841712", "name": "vscode-p0c3a", "state": "TIMEOUT", "exit_code": "0:15", "elapsed": "08:00:12",
             "end": "2026-10-23T06:12:40", "partition": "cpu", "requested_memory": "16G", "peak_memory_mb": 3400.0,
             "source": {"kind": "app", "label": "VS Code", "app": "vscode"}},
            {"id": "18841630", "name": "hpcrest-env_sync", "state": "COMPLETED", "exit_code": "0:0", "elapsed": "00:04:09",
             "end": "2026-10-22T19:44:03", "partition": "cpu", "requested_memory": "4G", "peak_memory_mb": 880.0,
             "source": {"kind": "agent", "label": "agent: env_sync", "token": "llm"}},
        ],
    },
    "hpclib-faster": {"queued": [], "running": [
        {"id": "4471023", "name": "opencode-a81kd", "state": "RUNNING", "reason": "None", "partition": "gpu", "elapsed": "0:55:10",
         "time_limit": "8:00:00", "nodes": "1", "cpus": "8", "gres": "gpu:a100:1", "memory": "64G", "node_list": "fc041",
         "start": "", "source": {"kind": "app", "label": "OpenCode", "app": "opencode"}}], "finished": []},
}
for v in JOBS.values():
    v["at"] = NOW
    v["counts"] = {"queued": len(v["queued"]), "running": len(v["running"]), "finished": len(v["finished"]),
                   "failed": sum(1 for j in v["finished"] if j["state"] != "COMPLETED" and j["source"]["kind"] in ("agent", "other"))}

KB = 1024 * 1024  # 1 GB in KB
STORAGE = {
    "hpclib-grace": {"scanned": NOW - 3 * 3600, "scan": {"state": "done"}, "work_dirs": ["/scratch/user/jdoe/llm"],
                     "quotas": {"host": "grace1", "checked": NOW - 2400, "environment": [],
                                "storage": {"command": "showquota", "source": "default", "exit_code": 0, "saved": None,
                                            "output": ["Your current disk quotas are:",
                                                       "Disk         Disk Usage      Limit    File Usage      Limit",
                                                       "/home/jdoe    3.1G        10.0G          9871      10000",
                                                       "/scratch/user/jdoe  612.4G  1.0T     41230     250000"]},
                                "project": {"command": "myproject", "source": "default", "exit_code": 0, "saved": None,
                                            "output": ["========================================================================",
                                                       "            List of jdoe's Project Accounts",
                                                       "------------------------------------------------------------------------",
                                                       "|  Account   | FY | Default | Allocation |Used & Pending SUs|   Balance  |",
                                                       "|132458962710|2026|      Y|   200000.00|          61244.75|   138755.25|",
                                                       "========================================================================"]}},
                     "quota": {"command": "showquota", "lines": [
                         "Your current disk quotas are:",
                         "Disk               Disk Usage      Limit    File Usage      Limit",
                         "/home/jdoe          2.1G         10.0G          9301       10000",
                         "/scratch/user/jdoe  612.4G        1.0T        598120     1000000"]},
                     "dirs": [
                         {"path": "/scratch/user/jdoe/llm", "kb": int(486.2 * KB), "files": 412880, "partial": False,
                          "children": [{"path": "/scratch/user/jdoe/llm/models", "kb": int(301.5 * KB)},
                                       {"path": "/scratch/user/jdoe/llm/runs", "kb": int(118.0 * KB)},
                                       {"path": "/scratch/user/jdoe/llm/envs", "kb": int(41.8 * KB)},
                                       {"path": "/scratch/user/jdoe/llm/pai", "kb": int(16.4 * KB)},
                                       {"path": "/scratch/user/jdoe/llm/inputs", "kb": int(6.1 * KB)},
                                       {"path": "/scratch/user/jdoe/llm/scratch", "kb": int(2.4 * KB)}]},
                         {"path": "/home/jdoe/.local/tunnels", "kb": int(1.3 * KB), "files": 5122, "partial": False,
                          "children": [{"path": "/home/jdoe/.local/tunnels/rest", "kb": int(0.9 * KB)},
                                       {"path": "/home/jdoe/.local/tunnels/sessions", "kb": int(0.3 * KB)},
                                       {"path": "/home/jdoe/.local/tunnels/settings", "kb": 220}]}]},
    "hpclib-faster": {"scanned": None, "scan": {"state": "idle"}, "dirs": [], "quota": None,
                      "work_dirs": ["/scratch/user/jdoe/llm"]},
    "hpclib-entropy": {"scanned": None, "scan": {"state": "idle"}, "dirs": [], "quota": None, "work_dirs": ["/home/jdoe/llm"]},
}

APPS = [{"id": "agents", "title": "Agents"}, {"id": "jupyter", "title": "JupyterLab", "kind": "tunnel"},
        {"id": "vscode", "title": "VS Code", "kind": "tunnel"}, {"id": "pai", "title": "PAI", "kind": "tunnel"},
        {"id": "opencode", "title": "OpenCode", "kind": "tunnel"}, {"id": "smb", "title": "Data transfer", "kind": "login"},
        {"id": "cmccdb", "title": "CMCC DB (dev)", "kind": "tunnel",
         "description": "The mechanochemistry database's development server, in a job on the cluster."},
        {"id": "cmccdb_preview", "title": "CMCCDB Preview", "kind": "tunnel",
         "tab": {"parent": "cmccdb", "id": "preview", "label": "Preview"}}]
PROPOSALS = [{"cluster": "hpclib-grace", "name": "jax_gpu_bench", "token": "llm", "proposed": NOW - 5400, "revised": False,
              "replaces_existing": False, "rationale": "GPU benchmark for the JAX force field; needs --gres=gpu:a100:1"},
             {"cluster": "hpclib-faster", "name": "xtb_opt", "token": "llm", "proposed": NOW - 86000, "revised": True,
              "replaces_existing": True, "rationale": "adds an array option for conformer batches"}]
APP_SETTINGS = {"tunnel_args": ["--time=7-00:00:00", "--mem=16gb"], "conda_env": None, "modules": [], "project": "",
                "python_env": False, "job": True, "installable": True, "install_modules": ["WebProxy"], "settings": {},
                "defaults": {}, "secrets": [{"name": "PAI_GITHUB_PAT", "label": "GitHub PAT (optional)", "hint": "", "set": True}],
                "fields": [{"name": "PAI_DIR", "label": "Install directory", "hint": "", "choices": None}], "tokens": None}
FILES = {"path": "/scratch/user/jdoe/llm", "type": "directory", "entries": [
    {"name": n, "type": t, "size": z, "mtime": NOW - a, "path": "/scratch/user/jdoe/llm/" + n}
    for n, t, z, a in [("models", "directory", 0, 86400 * 3), ("runs", "directory", 0, 1800), ("envs", "directory", 0, 86400 * 9),
                       ("pai", "directory", 0, 86400), ("inputs", "directory", 0, 7200), ("README.md", "file", 2140, 86400 * 20),
                       ("jax_bench.log", "file", 48211, 3000)]]}


def app_state(app, name):
    c = CLUSTERS[name]
    ports = {"jupyter": 31007, "vscode": 31006, "pai": 31003, "opencode": 31008, "smb": 31011, "cmccdb": 31020,
             "cmccdb_preview": 31021}
    base = {"app": app, "cluster": name, "host": c["host"], "port": ports[app], "token_known": True, "started_here": False, "url": None,
            "prompt": None, "shared": app == "pai", "kind": "tunnel", "login": c["login"], "tools": [], "controls": [],
            "state": "down", "install": {"state": "installed", "message": ""}}
    msgs = {"jupyter": ("installed", "jupyter 4.3.1 in /scratch/user/jdoe/llm/envs/lab"),
            "vscode": ("installed", "image /scratch/user/jdoe/images/vscode.sif (pulled 2026-09-30)"),
            "pai": ("installed", "installed: /scratch/user/jdoe/pai/proto-auto-interface (4c1e2aa 2026-10-18)"),
            "opencode": ("missing", "not installed: no opencode 1.15.12 in /scratch/user/jdoe/opencode"),
            "smb": ("unknown", "its install.sh can't check"),
            "cmccdb": ("installed", "checkout 9b31d0e on dev"), "cmccdb_preview": ("installed", "checkout 1f02c7a on preview")}
    st, msg = msgs[app]
    base["install"] = {"state": st, "message": msg, "checked": NOW - 600}
    if name == "hpclib-grace" and app == "pai":
        base.update(state="up", url="http://127.0.0.1:31003/", started_here=True, version="0.9.4",
                    instance={"job": "18841690", "node": "g117"})
    if name == "hpclib-grace" and app == "vscode":
        base.update(state="queued", status="waiting for resources (Priority)")
    if name == "hpclib-faster" and app == "opencode":
        base.update(state="up", url="http://127.0.0.1:31008/", install={"state": "installed", "message": "opencode 1.15.12"})
    if app == "pai":
        base["reinstall"] = {"label": "Update", "title": "fast-forward and pull the image"}
    return base


SERVER_CONFIG = {"path": "/home/jdoe/.local/tunnels/rest/config.json", "rules": {},
                 "config": {"limits": {"max_gpus": 1, "max_concurrent_jobs": 4},
                            "sandbox": {"method": "auto", "binds": ["/sw"], "gpus": "auto"}},
                 "effective": {"limits": {"max_time": "1-00:00:00", "max_mem": "128G", "max_cpus": 32, "max_nodes": 1,
                                          "max_gpus": 1, "max_concurrent_jobs": 4, "max_array_tasks": 1000},
                               "environments": {"managers": {"uv": {"available": True, "version": "0.9.2"},
                                                             "pixi": {"available": False}}},
                               "sandbox": {"effective": "singularity"}},
                 "editable": ["limits", "cluster_notes", "environments", "environment", "sandbox", "poll_interval"]}


PREFS = {"notifications": {"tunnel": True, "prompt": True, "job": True, "proposal": False},
         "new_cluster": {"connection_hours": 12, "work_dir_pattern": "/scratch/user/{user}/llm", "local_root_pattern": "~/Documents/hpclib-agents/{cluster}", "mcp_python": "", "sandbox_gpus": "auto",
                         "max_gpus": 1, "mcp_name_pattern": "hpclib-{cluster}"},
         "llm_clients": {"claude_desktop": True, "claude_code": True, "cursor": False},
         "interface": {"common": {"--radius-control": "8px"}, "light": {"--accent": "#7b3fbf"}, "dark": {}}}
PORTS = {"range": [20000, 32000], "default_range": [20000, 32000], "ports": [
    {"cluster": "hpclib-grace", "app": None, "title": "agent tunnel", "which": "port", "port": 20735, "status": "tunnel"},
    {"cluster": "hpclib-grace", "app": None, "title": "agent tunnel", "which": "process_port", "port": 23129, "status": "tunnel"},
    {"cluster": "hpclib-grace", "app": "pai", "title": "PAI", "which": "port", "port": 31003, "status": "tunnel"},
    {"cluster": "hpclib-grace", "app": "pai", "title": "PAI", "which": "process_port", "port": 28841, "status": "tunnel"},
    {"cluster": "hpclib-grace", "app": "vscode", "title": "VS Code", "which": "port", "port": 31006, "status": "taken"},
    {"cluster": "hpclib-grace", "app": "vscode", "title": "VS Code", "which": "process_port", "port": 25530, "status": "free"},
    {"cluster": "hpclib-faster", "app": None, "title": "agent tunnel", "which": "port", "port": 24410, "status": "free"},
    {"cluster": "hpclib-faster", "app": None, "title": "agent tunnel", "which": "process_port", "port": 29902, "status": "free"}]}
CLIENTS = [
    {"id": "claude_desktop", "title": "Claude Desktop", "path": "/Users/jdoe/Library/Application Support/Claude/claude_desktop_config.json",
     "available": True, "enabled": True, "after": "Quit and reopen Claude Desktop to load changed entries.", "error": None,
     "entries": [{"name": "hpclib-grace", "state": "current"}, {"name": "hpclib-faster", "state": "different"},
                 {"name": "hpclib-entropy", "state": "current"}], "stale": ["hpclib-terra"]},
    {"id": "claude_code", "title": "Claude Code", "path": "/Users/jdoe/.claude.json", "command": "/Users/jdoe/.local/bin/claude",
     "available": True, "enabled": True, "after": "", "error": None,
     "entries": [{"name": "hpclib-grace", "state": "current"}, {"name": "hpclib-faster", "state": "current"},
                 {"name": "hpclib-entropy", "state": "current"}], "stale": []},
    {"id": "cursor", "title": "Cursor", "path": "/Users/jdoe/.cursor/mcp.json", "available": False, "enabled": False,
     "after": "", "error": None, "entries": [{"name": "hpclib-grace", "state": "missing"}], "stale": []},
    {"id": "chatgpt", "title": "ChatGPT", "path": "/Users/jdoe/.codex/config.toml", "available": True, "enabled": False,
     "detail": "the desktop app, the Codex CLI and its IDE extension (they share config.toml)",
     "after": "", "error": None, "entries": [{"name": "hpclib-grace", "state": "missing"},
     {"name": "hpclib-faster", "state": "missing"}, {"name": "hpclib-entropy", "state": "missing"}], "stale": []}]
ABOUT = {"hpclib_version": "1.4.0", "hpclib_version_on_disk": "1.4.0", "restart_for": None, "console_version": "0.1",
         "hpclib_dir": "/Users/jdoe/Documents/Postdoc/Development/hpclib/hpclib", "git": {"branch": "main", "commit": "2ae3a51c09d1"},
         "python": "3.12.4", "python_path": "/opt/homebrew/bin/python3", "platform": "macOS-15.1-arm64-arm-64bit",
         "console_dir": "/Users/jdoe/.config/hpclib/console", "agents_dir": "/Users/jdoe/.config/hpclib/agents", "pid": 48121,
         "started": NOW - 7200, "clusters": 3, "packages": [{"name": "pai", "version": "0.2.5"}, {"name": "cmccdb", "version": "0.4.1"}],
         "url": "http://127.0.0.1:27180", "static": True}


WIDGET_DIR = ROOT / "examples" / "console-widgets" / "gpu-queue"
WIDGETS = [dict(w, key=f"gpu-queue/{w['id']}", package="gpu-queue", version="0.1.0")
           for w in json.loads((WIDGET_DIR / "hpclib-package.json").read_text())["widgets"]]


UPDATE = {"repo": "/Users/jdoe/hpclib", "git": True, "fetched": NOW - 60, "fetch_error": None, "branch": "master",
          "upstream": "origin/master", "remote_url": "https://github.com/Tabor-Research-Group/hpclib.git",
          "commit": "5011d93c2a41", "version": "1.4.0", "changed_files": [], "changed_count": 0, "behind": 3, "ahead": 0,
          "upstream_commit": "9f3c67e0b1d2", "upstream_version": "1.4.1", "reason": None, "can_update": True,
          "up_to_date": False, "running": "1.4.0",
          "incoming": [{"commit": "9f3c67e0b1d2", "time": NOW - 3600, "subject": "Docs: screenshots of the console"},
                       {"commit": "7905522aa1c3", "time": NOW - 7200, "subject": "Console Settings: Update hpclib"},
                       {"commit": "825bcd5e44f0", "time": NOW - 9000, "subject": "LLM clients: paths you choose"}]}


def answer(path, method):
    parts = path.split("/")
    if path == "widgets":
        return {"widgets": WIDGETS if SHOW_WIDGETS else []}
    if parts[0] == "widgets" and len(parts) == 3:
        return {"html": (WIDGET_DIR / "widgets" / "gpu-queue.html").read_text()}
    if path == "preferences":
        return PREFS
    if path == "ports":
        return PORTS
    if path == "llm-clients":
        return {"clients": CLIENTS}
    if path == "update":
        return UPDATE
    if path == "styles":
        return {"path": "/Users/jdoe/.config/hpclib/console/user.css", "exists": True,
                "values": {"common": {}, "light": {"--accent": "#7b3fbf"}, "dark": {}}}
    if path == "about":
        return ABOUT
    if path == "platform":
        return {"platform": "wsl", "title": "WSL (Linux on Windows)", "source": "detected", "detected": "wsl",
                "choices": ["auto", "linux", "macos", "wsl"], "setting": "auto", "env": None,
                "wsl_distro": "Ubuntu-24.04", "windows_home": "/mnt/c/Users/jdoe", "windows_home_source": "windows",
                "windows_home_setting": "", "opener": "explorer.exe"}
    if path == "watch":
        return {"at": NOW, "tunnels": []}
    if path == "jobs":
        return {"jobs": []}
    if path == "health":
        return {"hpclib_version": LOCAL}
    if path == "clusters":
        return {"clusters": list(CLUSTERS.values())}
    if path == "apps":
        return {"apps": APPS}
    if path in ("prompts",):
        return {"prompts": [], "running": False}
    if path == "proposals":
        return {"proposals": PROPOSALS, "clusters": {}}
    if parts[0] == "clusters" and len(parts) >= 2:
        name = parts[1]
        rest = parts[2:]
        if not rest:
            return CLUSTERS[name]
        if rest == ["jobs"]:
            return JOBS.get(name, {"queued": [], "running": [], "finished": [], "at": NOW,
                                   "counts": {"queued": 0, "running": 0, "finished": 0, "failed": 0}})
        if rest == ["storage"]:
            return STORAGE[name]
        if rest == ["settings"]:
            return {"modes": ["all", "new", "review"], "auto_approve_templates": "new",
                    "tunnel_args": ["--time=8:00:00", "--mem=2gb"], "connection_hours": 12, "rest_on": "job"}
        if rest == ["ports"]:
            return {"ports": [{"port": 20735, "what": "agent tunnel", "app": None, "running": True},
                              {"port": 31003, "what": "PAI", "app": "pai", "running": True},
                              {"port": 31006, "what": "VS Code", "app": "vscode", "running": False}]}
        if rest[:2] == ["rest", "files"]:
            return FILES
        if rest == ["rest", "actions"]:
            acts = [{"name": n, "title": t, "description": d, "mode": m, "scope": sc, "writes": w, "default": dflt,
                     "enabled": e, "permitted": True, "params": p}
                    for n, t, d, m, sc, w, dflt, e, p in (
                        ("list", "List a folder on the SMB server", "The files and folders in REMOTE, as rclone lsjson entries.",
                         "sync", "read", None, True, True, {"remote": {"type": "remote-path"}}),
                        ("get", "Copy from the SMB server", "Copies REMOTE into LOCAL, a new directory in your work directories, as a SLURM job.",
                         "job", "submit", "cluster", True, True, {"remote": {"type": "remote-path"},
                                                                  "local": {"type": "cluster-path", "access": "write"}}),
                        ("put", "Copy to the SMB server", "Copies LOCAL to REMOTE, a folder on the server, as a SLURM job. Off unless the cluster's owner turns it on.",
                         "job", "submit", "remote", False, False, {"local": {"type": "cluster-path", "access": "read"},
                                                                    "remote": {"type": "remote-path"}}))]
            return {"tunnels": [{"tunnel": "data-transfer", "title": "SMB server", "enabled": True, "actions": acts,
                                 "description": "Files on your group's SMB server, with your sign-in: a Kerberos ticket or the saved password from Data transfer.",
                                 "availability": {"available": True, "detail": {"host": "files.example.edu", "root": "group/shared",
                                                                                "ticket": {"principal": "jdoe@TAMU.EDU", "expires": "10/26/26 06:12:40"}}}}]}
        if rest[:3] == ["rest", "admin", "config"]:
            return SERVER_CONFIG
    if parts[0] == "apps" and len(parts) == 4 and parts[3] == "settings":
        return APP_SETTINGS
    if parts[0] == "apps" and len(parts) == 3:
        return app_state(parts[1], parts[2])
    return {}



# (file, hash, width, scheme, widgets, action, selector): selector shoots one element; None the viewport (or the
# whole page with full=True in the action's result)
def click(text):
    return lambda page: page.get_by_role("button", name=text, exact=True).first.click()


def add_form(page):
    page.get_by_role("button", name="Add cluster", exact=True).click()
    page.wait_for_timeout(300)
    page.locator(".add-cluster input").first.fill("jdoe@terra.hprc.tamu.edu")


def setup_panel(page):
    page.locator("hpc-cluster-row").first.locator("button:has-text('Setup')").first.click()


def client_change(page):
    page.locator("#llm-clients").get_by_role("button", name="Change", exact=True).first.click()


def menu(page):
    page.locator("#console-menu > summary").click()


DOCS = [
    ("clusters-list", "#/c", 1280, None, False, None, "main"),
    ("clusters-add", "#/c", 1280, None, False, add_form, "main"),
    ("clusters-phone", "#/c", 390, None, False, None, None),
    ("cluster-overview", "#/c/hpclib-grace/overview", 1280, None, False, None, "main"),
    ("cluster-jobs", "#/c/hpclib-grace/jobs", 1280, None, False, None, "main"),
    ("cluster-jobs-dark", "#/c/hpclib-grace/jobs", 1280, "dark", False, None, None),
    ("cluster-apps", "#/c/hpclib-grace/apps", 1280, None, False, None, "main"),
    ("cluster-installed", "#/c/hpclib-grace/installed", 1280, None, False, None, "main"),
    ("cluster-storage", "#/c/hpclib-grace/storage", 1280, None, False, None, "main"),
    ("cluster-quota-checks", "#/c/hpclib-grace/settings", 1280, None, False, None, "section.card:has(h3:has-text('Quota checks'))"),
    ("cluster-settings", "#/c/hpclib-grace/settings", 1280, None, False, None, None),
    ("cluster-agents", "#/c/hpclib-grace/agents", 1280, None, False, None, "main"),
    ("cluster-setup", "#/c/hpclib-grace/agents", 1280, None, False, setup_panel, "hpc-panel"),
    ("cluster-files", "#/c/hpclib-grace/files", 1280, None, False, None, None),
    ("app-pai", "#/c/hpclib-grace/apps/pai", 1280, None, False, None, "main"),
    ("app-package-tab", "#/c/hpclib-grace/apps/cmccdb_preview", 1280, None, False, None, None),
    ("proposals", "#/proposals", 1280, None, False, None, None),
    ("menu", "#/c", 1280, None, False, menu, {"x": 0, "y": 0, "width": 560, "height": 300}),
    ("menu-dark", "#/c", 1280, "dark", False, menu, {"x": 0, "y": 0, "width": 560, "height": 300}),
    ("interface-settings", "#/interface", 1280, None, False, None, None),
    ("console-settings", "#/console", 1280, None, False, None, None),
    ("console-llm-clients", "#/console", 1280, None, False, client_change, "#llm-clients"),
    ("console-this-machine", "#/console", 1280, None, False, None, "#machine"),
    ("console-update", "#/console", 1280, None, False, None, "#update"),
    ("about", "#/about", 1280, None, False, None, "main"),
    ("widgets-home", "#/c", 1280, None, True, None, "main"),
    ("widgets-overview", "#/c/hpclib-grace/overview", 1280, None, True, None, "main"),
    ("widgets-interface", "#/interface", 1280, None, True, None, "#widgets"),
]


def main():
    global SHOW_WIDGETS
    server = agent_console.ConsoleServer(("127.0.0.1", 0), "shot-key", None, static_dir=str(ROOT / "agent-console"))
    server.RequestHandlerClass.log_message = lambda *a: None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.port}"
    only = os.environ.get("ONLY")
    shots = [d for d in DOCS if not only or d[0].startswith(tuple(only.split(",")))]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, hash_, width, scheme, show, act, selector in shots:
            SHOW_WIDGETS = show
            page = browser.new_page(viewport={"width": width, "height": 860 if width > 500 else 780},
                                    color_scheme=scheme or "light", device_scale_factor=1)
            page.route("**/api/**", lambda route: route.fulfill(
                body=json.dumps(answer(route.request.url.split("/api/", 1)[1].split("?", 1)[0], route.request.method)),
                content_type="application/json"))
            page.goto(base + "/#key=shot-key")
            page.wait_for_function("document.querySelector('#apps a') !== null")
            page.goto(base + "/" + hash_)
            page.wait_for_timeout(2500 if show else 1500)
            if act:
                act(page)
                page.wait_for_timeout(500)
            if re.search(r"\bnull\b|undefined|NaN", page.locator("main").inner_text()):
                print(f"WARNING {name}: the page shows null/undefined/NaN")
            path = str(OUT / f"{name}.png")
            if isinstance(selector, dict):
                page.screenshot(path=path, clip=selector)
            elif selector:
                page.locator(selector).first.screenshot(path=path)
            else:
                page.screenshot(path=path)
            page.close()
            shrink(path)
        browser.close()
    server.shutdown()


def shrink(path):
    """256 colours look the same for these pages, at a third of the size."""
    try:
        from PIL import Image
    except ImportError:
        return
    im = Image.open(path).convert("RGB")
    im.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(path, optimize=True)


main()
