"""
The files API: what the file browser, the editor and the viewers ask of the disk. Everything is the user's own
permissions (the server is the user), so nothing here guards a path beyond refusing to delete / or the home directory.

  Files(root, edit_limit, confirm)
    .list(path, hidden)           a directory: {path, parent, entries, truncated, hidden, writable}
    .stat(path)                   {path, type, size, mtime, mtime_ns, writable}
    .write(path, stream, length, mtime_ns, how)   through a temporary file and a rename, keeping the mode; 409 when
                                  the file changed since mtime_ns
    .mkdir(path)  .rename(src, dst)  .delete(path)   (delete: a file, a link or an empty directory)
    .usage(path, limit)           a directory's folders by size and file count, at low priority, within a time limit
    .tail(path, offset, ident)    the bytes a file has gained since offset: the follower for logs, a build's output
                                  and the editor's "this file changed on disk"

Python 3.6 or newer, standard library only.
"""
import os
import stat
import subprocess
import tempfile
import time

from .common import Refused, oserror, resolve

MAX_ENTRIES = 5000
CHUNK = 1 << 16


def kind(st):
    if stat.S_ISDIR(st.st_mode):
        return "directory"
    if stat.S_ISREG(st.st_mode):
        return "file"
    return "other"


def entry(path):
    lst = os.lstat(path)
    out = {"name": os.path.basename(path) or path, "size": lst.st_size, "mtime": lst.st_mtime,
           "mode": stat.filemode(lst.st_mode)}
    if stat.S_ISLNK(lst.st_mode):
        out["type"] = "link"
        try:
            out["target"] = os.readlink(path)
            st = os.stat(path)
            out["target_type"] = kind(st)
            out["size"] = st.st_size
        except OSError:
            out["target_type"] = "missing"
    else:
        out["type"] = kind(lst)
    return out


def _umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


def default_mode():
    """The mode a new file gets: what a shell would give it (0666 less the umask)."""
    return 0o666 & ~_umask()


def write_atomic(path, stream, length, mode=None, exclusive=False):
    """`length` bytes of `stream` to path through a temporary file beside it and a rename. `exclusive`: only if path
    doesn't exist at that moment (a hard link fails where a rename would replace), so two creates of one name can't both win."""
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".workspace-tmp",
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as f:
            left = length
            while left > 0:
                data = stream.read(min(CHUNK, left))
                if not data:
                    raise Refused(400, "the upload ended early")
                f.write(data)
                left -= len(data)
        os.chmod(tmp, mode if mode is not None else default_mode())
        if exclusive:
            try:
                os.link(tmp, path)
            except FileExistsError:
                raise Refused(409, "{} already exists".format(path))
            os.unlink(tmp)
        else:
            os.rename(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Files:
    USAGE_LIMIT = 600
    TAIL_FIRST = 16 * 1024
    TAIL_MAX = 256 * 1024

    def __init__(self, root, edit_limit, confirm):
        self.root, self.edit_limit, self.confirm = root, edit_limit, confirm

    def list(self, path, hidden=False):
        path = resolve(path)
        try:
            names = os.listdir(path)
        except OSError as e:
            raise oserror(e, path)
        entries, truncated = [], False
        for name in sorted(names, key=lambda n: n.lower()):
            if name.startswith(".") and not hidden:
                continue
            if len(entries) >= MAX_ENTRIES:
                truncated = True
                break
            try:
                entries.append(entry(os.path.join(path, name)))
            except OSError:
                continue
        return {"path": path, "parent": os.path.dirname(path) if path != "/" else None, "entries": entries,
                "truncated": truncated, "hidden": sum(1 for n in names if n.startswith(".")),
                "writable": os.access(path, os.W_OK)}

    def stat(self, path):
        path = resolve(path)
        try:
            st = os.stat(path)
        except OSError as e:
            raise oserror(e, path)
        return {"path": path, "type": kind(st), "size": st.st_size, "mtime": st.st_mtime,
                "mtime_ns": str(st.st_mtime_ns), "writable": os.access(path, os.W_OK)}

    def write(self, path, stream, length, mtime_ns=None, how="any"):
        path = resolve(path)
        if how not in ("create", "replace", "any"):
            raise Refused(400, "how: create, replace or any")
        try:
            st = os.stat(path)
        except FileNotFoundError:
            st = None
        except OSError as e:
            raise oserror(e, path)
        if st is not None and not stat.S_ISREG(st.st_mode):
            raise Refused(400, "{} isn't a regular file".format(path))
        if how == "create" and st is not None:
            raise Refused(409, "{} already exists".format(path))
        if how == "replace" and st is None:
            raise Refused(404, "{} doesn't exist (any more)".format(path))
        if mtime_ns and st is not None and str(st.st_mtime_ns) != mtime_ns:
            raise Refused(409, "{} changed since it was opened".format(path))
        if length > max(self.edit_limit, 1 << 30):
            raise Refused(413, "that's larger than the Workspace takes")
        target = os.path.realpath(path) if os.path.islink(path) else path    # keep a link, write its file
        try:
            write_atomic(target, stream, length, stat.S_IMODE(st.st_mode) if st is not None else None, exclusive=how == "create")
        except OSError as e:
            raise oserror(e, path)
        return self.stat(path)

    def mkdir(self, path):
        path = resolve(path)
        if os.path.lexists(path):
            raise Refused(409, "{} already exists".format(path))
        try:
            os.makedirs(path)
        except OSError as e:
            raise oserror(e, path)
        return self.stat(path)

    def rename(self, src, dst):
        src, dst = resolve(src), resolve(dst)
        if os.path.lexists(dst):
            raise Refused(409, "{} already exists".format(dst))
        if not os.path.lexists(src):
            raise Refused(404, "{} doesn't exist".format(src))
        try:
            os.rename(src, dst)
        except OSError as e:
            raise oserror(e, src)
        return {"from": src, "to": dst}

    def delete(self, path):
        path = resolve(path)
        if path in ("/", os.path.expanduser("~"), self.root):
            raise Refused(400, "not that one")
        try:
            if stat.S_ISDIR(os.lstat(path).st_mode):
                os.rmdir(path)
            else:
                os.unlink(path)
        except OSError as e:
            raise oserror(e, path)
        return {"deleted": path}

    # -- disk usage ------------------------------------------------------------------------------------------------

    @staticmethod
    def _du(args, path, limit):
        """{path: number} from du --max-depth=1 (and whether it finished), at low priority with a time limit."""
        try:
            res = subprocess.run(["timeout", str(limit), "nice", "-n", "19", "du", "--max-depth=1"] + args + ["--", path],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                 universal_newlines=True, timeout=limit + 30)
        except (OSError, subprocess.TimeoutExpired):
            return {}, False, ""
        out = {}
        for line in res.stdout.splitlines():
            value, _, p = line.partition("\t")
            if value.isdigit():
                out[p] = int(value)
        return out, res.returncode == 0, res.stderr

    @staticmethod
    def _count(path, deadline):
        """{path: entries} for path and each folder in it by walking (du too old for --inodes): files, folders and
        links, each folder counting itself, as du --inodes does."""
        out, done = {}, True

        def walk(top):
            n = 1
            for base, dirs, files in os.walk(top, onerror=lambda e: None):
                n += len(dirs) + len(files)
                if time.time() > deadline:
                    return n, False
            return n, True
        total = 1
        try:
            names = os.listdir(path)
        except OSError as e:
            raise oserror(e, path)
        for name in names:
            p = os.path.join(path, name)
            if os.path.isdir(p) and not os.path.islink(p):
                n, ok = walk(p)
                out[p] = n
                total += n
                done = done and ok
            else:
                total += 1
            if not done:
                break
        out[path] = total
        return out, done

    def usage(self, path, limit=120):
        """A directory's folders by size (du -k) and by file count (du --inodes, else a walk): the Storage tab's
        scan, for any directory. {path, kb, files, partial, children: [{name, path, kb, files}], here: {...}}."""
        path = resolve(path)
        if not os.path.isdir(path):
            raise Refused(404, "{} isn't a directory".format(path))
        limit = max(5, min(int(limit), self.USAGE_LIMIT))
        start = time.time()
        sizes, sized, _ = self._du(["-k"], path, limit)
        counts, counted, err = self._du(["--inodes"], path, max(5, int(limit - (time.time() - start))))
        method = "du --inodes"
        if not counts and ("inodes" in err or "unrecognized" in err or "invalid option" in err):
            counts, counted = self._count(path, time.time() + max(5, limit - (time.time() - start)))
            method = "walk"
        children = []
        for p in sorted(set(sizes) | set(counts)):
            if os.path.normpath(p) == path:
                continue
            children.append({"name": os.path.basename(p), "path": os.path.normpath(p), "kb": sizes.get(p),
                             "files": counts.get(p)})
        kb, files = sizes.get(path), counts.get(path)
        here = None
        if kb is not None and files is not None:
            # what is directly in the directory: the total less its folders (and the folder itself, for files)
            here = {"kb": max(0, kb - sum(c["kb"] or 0 for c in children)),
                    "files": max(0, files - 1 - sum(c["files"] or 0 for c in children))}
        return {"path": path, "kb": kb, "files": files, "partial": not (sized and counted), "children": children,
                "here": here, "count_method": method, "seconds": round(time.time() - start, 1), "limit": limit}

    # -- following a file as it grows ------------------------------------------------------------------------------

    def tail(self, path, offset=None, ident=None):
        """The new bytes of a growing file after `offset` (the last 16 KiB when none is given):
        {exists, size, offset, id, data, restarted}. `id` names the file (device and inode): a client that sends back
        the id it was given learns when the file was replaced (a log rotated, a job's output recreated), and starts
        again from its end with restarted true. A file that shrank is treated the same way."""
        path = resolve(path)
        try:
            st = os.stat(path)
        except OSError:
            return {"exists": False, "size": 0, "offset": 0, "id": None, "data": "", "restarted": False}
        if not stat.S_ISREG(st.st_mode):
            raise Refused(400, "{} isn't a regular file".format(path))
        now_id = "{}:{}".format(st.st_dev, st.st_ino)
        size, restarted = st.st_size, False
        if offset is not None and (offset < 0 or offset > size or (ident and ident != now_id)):
            restarted = True
        if offset is None or restarted:
            offset = max(0, size - self.TAIL_FIRST)
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                raw = f.read(min(size - offset, self.TAIL_MAX))
        except OSError as e:
            raise oserror(e, path)
        return {"exists": True, "size": size, "offset": offset + len(raw), "id": now_id,
                "data": raw.decode("utf-8", "replace"), "restarted": restarted}
