#!/usr/bin/env python3
"""
fsdiag: the filesystem diagnostics from a shell, for when the console isn't at hand. Same measurements and the same
findings as the Filesystem diagnostics app, printed as a report you can paste into a message.

  fsdiag_cli.py probe [fsprobe.py options]      time file system calls here (this node), count folders, read quotas
  fsdiag_cli.py requests [--hours H] [--slow-ms N]   what the REST server's slow requests were waiting for
  fsdiag_cli.py all [fsprobe.py options]        both, in one report

A leading --json prints {"node", "problems", "findings", "report"} instead of the report alone: what the tunnel actions
(actions.json) hand to an agent.

Run `probe` on a login node as you are, and inside a job (srun --pty, or sbatch --wrap) to measure a compute node;
the app's "Compute node" choice does that for you. Options after the command go to fsprobe.py (--path LABEL=PATH,
--survey DIR, --repeats N, --no-quota, --no-latency...; see fsprobe.py --help). Exit status: 0 when nothing was
wrong, 1 when a problem was found, 2 for a bad command line.

Python 3.6+, standard library only.
"""
import argparse
import json
import os
import socket
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fsfindings  # noqa: E402
import fsprobe  # noqa: E402
import fsrequests  # noqa: E402


def probe(argv):
    """The probe's result (a dict), run in this process with fsprobe's own options."""
    fd, out = tempfile.mkstemp(prefix="fsdiag-", suffix=".json")
    os.close(fd)
    try:
        code = fsprobe.main(list(argv) + ["--out", out])
        if code not in (0, None):
            raise SystemExit(code)
        with open(out) as f:
            return json.load(f)
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def requests(hours, slow_ms):
    data = os.path.expanduser(os.environ.get("HPCTUNNELS_DATA_DIR") or "~/.local/tunnels")
    return fsrequests.load(data, hours, slow_ms)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    as_json = argv[0] == "--json"
    if as_json:
        argv = argv[1:] or ["--help"]
    command, rest = argv[0], argv[1:]
    if command not in ("probe", "requests", "all"):
        print("fsdiag: unknown command {!r} (probe, requests or all)".format(command), file=sys.stderr)
        return 2
    results, analysis = [], None
    if command in ("requests", "all"):
        p = argparse.ArgumentParser(prog="fsdiag_cli.py requests")
        p.add_argument("--hours", type=float, default=24.0)
        p.add_argument("--slow-ms", type=float, default=fsrequests.SLOW_MS)
        if command == "requests":
            opts = p.parse_args(rest)
        else:   # "all" takes fsprobe's options; the request window is the default one
            opts = p.parse_args([])
        analysis = requests(opts.hours, opts.slow_ms)
    if command in ("probe", "all"):
        results = [probe(rest)]
    found = fsfindings.findings(results, analysis)
    title = "File system diagnostics for {}@{}".format(os.environ.get("USER", "?"), socket.gethostname())
    report = fsfindings.report_text(title, results, found, analysis)
    problems = sum(1 for f in found if f["severity"] == "problem")
    if as_json:
        print(json.dumps({"node": os.environ.get("SLURMD_NODENAME") or socket.gethostname(), "problems": problems,
                          "findings": found, "report": report}))
        return 0       # the answer is the findings; a problem found is not a failure to run
    print(report)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
