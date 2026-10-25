#!/usr/bin/env python3
"""
Moving files between an agent's directories and a tunnel action's private staging directory, without following
symbolic links. Tunnel actions (rest_actions.py) run as you, outside the agents' sandbox, but read from or write to
directories an agent can change while the action's job waits in the queue. Paths checked when the action was asked
for could by then be links to ~/.ssh or a shell startup file; so an action's job never hands the agent's path to
its tool (rclone, ...). It copies through this helper instead:

  action_paths.py stage-in SRC STAGING --scope FILE    copy SRC (a file or a directory) into STAGING/NAME
  action_paths.py place STAGING DEST --scope FILE      copy STAGING's contents into DEST, a new directory

Both walk every path from / one component at a time with O_NOFOLLOW, refuse one that leaves the scope's `roots` or
enters its `deny` paths, and hold directories open while they work: a link swapped in later is never followed. A
link inside a copied tree is skipped (and reported), never followed. DEST must not exist yet: place makes it, so
nothing in it was put there by anyone else, and creates each file with O_EXCL.

The scope FILE is JSON, {"roots": [...], "deny": [...]}, written by the REST server for this action only.
Prints one JSON line: {"files", "bytes", "skipped": [...]}; exits 1 (with the reason on standard error) on refusal.
Standard library only.
"""
import json
import os
import stat
import sys

CHUNK = 1 << 20
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


class Refused(Exception):
    pass


def _inside(root, path):
    return os.path.commonpath([root, path]) == root


def check_scope(path, scope):
    if any(_inside(d, path) for d in scope.get("deny") or []):
        raise Refused(f"{path} is a protected path")
    roots = scope.get("roots")
    if roots is not None and not any(_inside(r, path) for r in roots):
        raise Refused(f"{path} is outside the agent's directories")


def walk_open(path, scope, last_dir=True):
    """
    Open `path` (absolute, normalized) component by component without following links. Returns (parent fd, name,
    fd of the last component or None if it doesn't exist). The last component must be a directory when last_dir.
    """
    if not os.path.isabs(path) or os.path.normpath(path) != path or path == "/":
        raise Refused(f"{path}: an absolute, normalized path is needed")
    check_scope(path, scope)
    parts = path.strip("/").split("/")
    fd = os.open("/", DIR_FLAGS)
    try:
        for part in parts[:-1]:
            try:
                nxt = os.open(part, DIR_FLAGS, dir_fd=fd)
            except OSError as e:
                raise Refused(f"{path}: {part} is not a directory (or is a link): {e.strerror}") from e
            os.close(fd)
            fd = nxt
        name = parts[-1]
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return fd, name, None
        if stat.S_ISLNK(st.st_mode):
            raise Refused(f"{path} is a link")
        if stat.S_ISDIR(st.st_mode):
            return fd, name, os.open(name, DIR_FLAGS, dir_fd=fd)
        if last_dir:
            raise Refused(f"{path} is not a directory")
        if not stat.S_ISREG(st.st_mode):
            raise Refused(f"{path} is not a regular file")
        return fd, name, os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    except BaseException:
        os.close(fd)
        raise


class Copier:
    def __init__(self):
        self.files = self.bytes = 0
        self.skipped = []

    def copy_file(self, src_fd, dst_dir, name, mode):
        out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode & 0o777 | 0o600, dir_fd=dst_dir)
        try:
            while True:
                chunk = os.read(src_fd, CHUNK)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    view = view[os.write(out, view):]
                self.bytes += len(chunk)
        finally:
            os.close(out)
        self.files += 1

    def copy_tree(self, src_dir, dst_dir, where):
        for name in sorted(os.listdir(src_dir)):
            st = os.stat(name, dir_fd=src_dir, follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                sub = os.open(name, DIR_FLAGS, dir_fd=src_dir)
                try:
                    os.mkdir(name, 0o700, dir_fd=dst_dir)
                    out = os.open(name, DIR_FLAGS, dir_fd=dst_dir)
                    try:
                        self.copy_tree(sub, out, f"{where}/{name}")
                        os.chmod(out, st.st_mode & 0o777 | 0o700)
                    finally:
                        os.close(out)
                finally:
                    os.close(sub)
            elif stat.S_ISREG(st.st_mode):
                src = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=src_dir)
                try:
                    self.copy_file(src, dst_dir, name, st.st_mode)
                finally:
                    os.close(src)
            else:
                self.skipped.append(f"{where}/{name}")      # links, sockets, devices: never followed

    def report(self):
        return {"files": self.files, "bytes": self.bytes, "skipped": self.skipped[:200]}


def stage_in(src, staging, scope):
    """Copy SRC (in the agent's directories) into STAGING/NAME (ours)."""
    parent, name, fd = walk_open(src, scope, last_dir=False)
    os.close(parent)
    if fd is None:
        raise Refused(f"{src} does not exist")
    copier = Copier()
    out_dir = os.open(staging, DIR_FLAGS)
    try:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            os.mkdir(name, 0o700, dir_fd=out_dir)
            sub = os.open(name, DIR_FLAGS, dir_fd=out_dir)
            try:
                copier.copy_tree(fd, sub, name)
            finally:
                os.close(sub)
        else:
            copier.copy_file(fd, out_dir, name, os.fstat(fd).st_mode)
    finally:
        os.close(fd)
        os.close(out_dir)
    return copier.report()


def place(staging, dest, scope):
    """Copy STAGING's contents (ours) into DEST, a new directory in the agent's directories."""
    parent, name, fd = walk_open(dest, scope)
    try:
        if fd is not None:
            os.close(fd)
            raise Refused(f"{dest} exists: an action copies into a new directory")
        os.mkdir(name, 0o755, dir_fd=parent)
        out = os.open(name, DIR_FLAGS, dir_fd=parent)
    finally:
        os.close(parent)
    src = os.open(staging, DIR_FLAGS)
    copier = Copier()
    try:
        copier.copy_tree(src, out, os.path.basename(dest))
    finally:
        os.close(src)
        os.close(out)
    return copier.report()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 5 or argv[0] not in ("stage-in", "place") or argv[3] != "--scope":
        print(__doc__, file=sys.stderr)
        return 2
    cmd, a, b, _, scope_file = argv
    try:
        with open(scope_file) as f:
            scope = json.load(f)
        if not isinstance(scope, dict):
            raise ValueError("not an object")
        scope = {"roots": [os.path.realpath(r) for r in scope["roots"]] if scope.get("roots") is not None else None,
                 "deny": [os.path.realpath(d) for d in scope.get("deny") or []]}
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"action_paths: can't read the scope {scope_file}: {e}", file=sys.stderr)
        return 2
    try:
        out = stage_in(a, b, scope) if cmd == "stage-in" else place(a, b, scope)
    except (Refused, OSError) as e:
        print(f"action_paths: refused: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
