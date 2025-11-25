
from node_comm import *

class GitHandler(NodeCommHandler):

    DEFAULT_CONNECTION = None
    DEFAULT_PORT_ENV_VAR = 'GIT_SOCKET_PORT'
    # DEFAULT_CONNECTION = os.path.expanduser("~/.gitsocket")
    def get_methods(self) -> 'dict[str,method]':
        return {
            'git':self.do_git
        }
    def do_git(self, args):
        return self.subprocess_response("git", args)


if __name__ == "__main__":
    import sys, os

    ppid = os.environ.get("PARENT_PROCESS_ID")
    if ppid is not None:
        curpid = os.environ.get("WORKER_PROCESS_ID", os.getpid())
        setup_parent_terminated_listener(ppid, curpid)

    port = os.environ.get(GitHandler.DEFAULT_PORT_ENV_VAR, os.environ.get("SESSION_ID"))
    if port is None:
        raise ValueError(f"`{GitHandler.DEFAULT_PORT_ENV_VAR}` must be set at the environment level")
    port = GitHandler.get_valid_port(port)

    GIT_CONNECTION = ('localhost', GitHandler.get_valid_port(port))
    # GitHandler.DEFAULT_CONNECTION = os.environ.get("GIT_SOCKET_FILE", GitHandler.DEFAULT_CONNECTION)
    if len(sys.argv) == 1:
        try:
            GitHandler.start_server(connection=GIT_CONNECTION)
        except OSError: # server exists
            pass
    else:
        GitHandler.client_request(sys.argv[1], sys.argv[2:], connection=GIT_CONNECTION)