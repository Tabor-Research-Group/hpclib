"""
The cluster as Slurm and `module` report it: partitions, GPU types, accounts (clusterinfo does the parsing, and is shared
with the REST server), and the module names a job may load. Both are cached, since asking is slow on a busy login node.

  Cluster(shell, user, hpclib)
    .get(refresh=False)       {slurm, partitions, gpus, accounts, ..., hpclib}, cached five minutes
    .modules(query)           {modules: [names containing query], available}, cached ten minutes

Programs run directly with the login shell's environment (ShellEnv.run); only `module`, a shell function, needs a shell.

Python 3.6 or newer, standard library only.
"""
import time

from .common import Refused

try:
    import clusterinfo
except ImportError:                       # imported as a package from the repository
    from hpclib.servers import clusterinfo


class Cluster:
    ASK_SECONDS = 20                  # each question to Slurm: a controller that is slow to answer is a problem to report, not to wait out
    CLUSTER_SECONDS = 300
    MODULES_SECONDS = 600

    def __init__(self, shell, user, hpclib):
        self.shell, self.user, self.hpclib = shell, user, hpclib
        self._cluster = None
        self._modules = None

    def forget(self):
        self._cluster = self._modules = None

    def get(self, refresh=False):
        now = time.time()
        if refresh or self._cluster is None or now - self._cluster["at"] > self.CLUSTER_SECONDS:
            def ask(argv):
                try:
                    return self.shell.run(argv, timeout=self.ASK_SECONDS)
                except Refused:
                    return 124, "{} didn't answer within {} s".format(argv[0], self.ASK_SECONDS)
            began = time.time()
            self._cluster = clusterinfo.collect(ask, self.user)
            self._cluster["hpclib"] = self.hpclib
            self._cluster["took"] = round(time.time() - began, 2)       # shown on the Submit form: where a slow open goes
        return self._cluster

    def modules(self, query=""):
        now = time.time()
        if self._modules is None or now - self._modules[0] > self.MODULES_SECONDS:
            code, out = self.shell.login("module -t avail 2>&1", timeout=60)
            names = []
            if code == 0:
                for line in out.splitlines():
                    line = line.strip()
                    if line and not line.endswith(":") and " " not in line and not line.startswith("/"):
                        names.append(line.rstrip("/"))
            self._modules = (now, sorted(set(names)))
        query = (query or "").lower()
        return {"modules": [m for m in self._modules[1] if query in m.lower()][:50], "available": bool(self._modules[1])}
