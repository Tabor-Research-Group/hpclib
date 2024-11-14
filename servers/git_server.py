
from node_comm import *

class GitHandler(NodeCommHandler):

    DEFAULT_CONNECTION = None
    # DEFAULT_CONNECTION = os.path.expanduser("~/.gitsocket")
    def get_methods(self) -> 'dict[str,method]':
        return {
            'git':self.do_git
        }
    def do_git(self, args):
        return self.subprocess_response("git", args)
    
    @staticmethod
    def get_valid_port(git_port, min_port=10000, max_port=65535):
        git_port = int(git_port)
        if git_port > max_port:
            git_port = git_port % max_port
        if git_port < min_port:
            git_port = max_port - (git_port % (max_port - min_port))
        return git_port


if __name__ == "__main__":
    import sys, os

    git_port = os.environ.get("GIT_SOCKET_PORT", os.environ.get("SESSION_ID"))
    if git_port is None:
        raise ValueError("`GIT_SOCKET_PORT` must be set at the environment level")
    git_port = GitHandler.get_valid_port(git_port)
    # GitHandler.DEFAULT_CONNECTION = os.environ.get("GIT_SOCKET_FILE", GitHandler.DEFAULT_CONNECTION)
    if len(sys.argv) == 1:
        GitHandler.DEFAULT_CONNECTION = ('', git_port)
        try:
            GitHandler.start_server()
        except OSError: # server exists
            pass
    else:
        git_host = os.environ.get("GIT_SOCKET_HOST")
        GitHandler.DEFAULT_CONNECTION = (git_host, git_port)
        GitHandler.client_request(sys.argv[1], sys.argv[2:])