"""What every part of the Workspace server shares: Refused, the path a page sends, the data directory."""
import os


class Refused(Exception):
    """A request that can't be done: `status` is the HTTP status the page gets, the message its text."""

    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status


MAX_JSON = 1 << 20


def data_dir():
    """$HPCTUNNELS_DATA_DIR (default ~/.local/tunnels): hpclib's per-user state on a cluster."""
    return os.path.expanduser(os.environ.get("HPCTUNNELS_DATA_DIR") or "~/.local/tunnels")


def state_dir():
    """Where the Workspace keeps its own: runs, operation logs, remembered form values."""
    return os.path.join(data_dir(), "workspace")


def templates_dir():
    return os.path.join(data_dir(), "script_templates")


def resolve(path):
    """The absolute, normalised path for what a page sent (~ and relative names are the user's home)."""
    if not path or not isinstance(path, str):
        raise Refused(400, "no path")
    if "\0" in path or len(path) > 4096:
        raise Refused(400, "that isn't a path")
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(os.path.expanduser("~"), path)
    return os.path.normpath(path)


def oserror(e, what):
    """The Refused an OSError on `what` should become."""
    if isinstance(e, FileNotFoundError):
        return Refused(404, "{} doesn't exist".format(what))
    if isinstance(e, PermissionError):
        return Refused(403, "{}: permission denied".format(what))
    if isinstance(e, NotADirectoryError):
        return Refused(400, "{} isn't a directory".format(what))
    if isinstance(e, IsADirectoryError):
        return Refused(400, "{} is a directory".format(what))
    if getattr(e, "errno", None) in (39, 66):
        return Refused(409, "{} isn't empty".format(what))
    return Refused(500, "{}: {}".format(what, e.strerror or e))
