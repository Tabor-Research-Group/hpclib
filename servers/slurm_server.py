
from node_comm import *

class SLURMHandler(NodeCommHandler):

    DEFAULT_PORT_ENV_VAR = 'SLURM_SOCKET_PORT'

    def get_methods(self) -> 'dict[str,method]':
        return {
            'sbatch':self.do_sbatch,
            'squeue':self.do_squeue,
        }
    def do_sbatch(self, args):
        return self.subprocess_response("sbatch", args)
    def do_squeue(self, args):
        return self.subprocess_response("squeue", args)
    @classmethod
    def stop_server(cls, args):
        cls.SLURM_SERVING = False
        raise KeyboardInterrupt

if __name__ == "__main__":
    import sys, os

    ppid = os.environ.get("PARENT_PROCESS_ID")
    if ppid is not None:
        curpid = os.environ.get("WORKER_PROCESS_ID", os.getpid())
        setup_parent_terminated_listener(ppid, curpid)

    port = os.environ.get(SLURMHandler.DEFAULT_PORT_ENV_VAR, os.environ.get("SESSION_ID"))
    if port is None:
        raise ValueError(f"`{SLURMHandler.DEFAULT_PORT_ENV_VAR}` must be set at the environment level")
    port = SLURMHandler.get_valid_port(port)

    SLURM_CONNECTION = ('localhost', SLURMHandler.get_valid_port(port))
    if len(sys.argv) == 1:
        try:
            SLURMHandler.start_server(connection=SLURM_CONNECTION)
        except OSError: # server exists
            print(f"Already serving on {SLURM_CONNECTION}")
            pass
    else:
        SLURMHandler.client_request(sys.argv[1], sys.argv[2:], connection=SLURM_CONNECTION)