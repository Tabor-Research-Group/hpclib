
from node_comm import *

class SLURMHandler(NodeCommHandler):

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
    import sys
    
    if len(sys.argv) == 1:
        try:
            SLURMHandler.start_server()
        except OSError: # server exists
            print(f"Already serving on {SLURMHandler.DEFAULT_CONNECTION}")
            pass
    else:
        SLURMHandler.client_request(sys.argv[1], sys.argv[2:])