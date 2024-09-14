
from node_comm import *
import os

class GitHandler(NodeCommHandler):

    DEFAULT_CONNECTION = os.path.expanduser("~/.gitsocket")
    def get_methods(self) -> 'dict[str,method]':
        return {
            'git':self.do_git
        }
    def do_git(self, args):
        return self.subprocess_response("git", args)

if __name__ == "__main__":
    import sys
    GitHandler.DEFAULT_CONNECTION = os.environ.get("GIT_SOCKET_FILE", GitHandler.DEFAULT_CONNECTION)
    if len(sys.argv) == 1:
        GitHandler.start_server()
    else:
        GitHandler.client_request(sys.argv[1], sys.argv[1:])