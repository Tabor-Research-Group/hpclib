#!/usr/bin/env python3
"""Scoped Data transfer actions for cluster-side sync metadata; Python 3.6-safe.

capabilities | inspect | preview | setup | settings
Read operations do not create root files. Mutations need a matching preview, explicit
confirmation, files:write scope, and a scope file affirming server file changes are enabled.
The local console's registrations, transfers, Git history and branches are never changed.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import stat
import sys
import uuid

from action_paths import Refused, walk_open
from sync_manager import read_setup_file
from sync_spec import (METADATA_FILE, SyncError, ignore_setup_plan, setup_metadata, validate_metadata)


def preview(root, fd):
    info = os.fstat(fd)
    ignored, ignored_mode = read_setup_file(root, '.gitignore', 1 << 20, dir_fd=fd)
    metadata, metadata_mode = read_setup_file(root, METADATA_FILE, 1 << 16, dir_fd=fd)
    plan, merged = ignore_setup_plan(ignored, metadata, {'root': [root, info.st_dev, info.st_ino]})
    plan['context'] = 'cluster-side metadata only; local console registrations are unchanged'
    plan['transfers_enabled'] = False
    return plan, merged, ignored_mode, metadata_mode


@contextlib.contextmanager
def mutation_lock(state, root):
    directory = os.path.join(state, 'sync', 'setup-locks')
    os.makedirs(directory, mode=0o700, exist_ok=True)
    name = hashlib.sha256(root.encode()).hexdigest() + '.lock'
    fd = os.open(os.path.join(directory, name), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_text(fd, name, text, mode):
    """Anchor the replace to the scoped, held directory; never follow an endpoint symlink."""
    temp = '.hpclib-sync-' + uuid.uuid4().hex
    out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(out, 'wb') as handle:
            handle.write(text.encode('utf-8'))
            handle.flush()
            os.fchmod(handle.fileno(), mode if mode is not None else 0o600)
            os.fsync(handle.fileno())
        os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temp, dir_fd=fd)
        except FileNotFoundError:
            pass


def execute(args):
    if args.operation == 'capabilities':
        return {'ready': True, 'protocol': 1, 'operations': ['inspect', 'preview', 'setup', 'settings'],
                'transfers_enabled': False}
    if not args.scope or not args.root:
        raise SyncError(422, 'an action requires its server scope file and an existing root')
    with open(args.scope) as handle:
        scope = json.load(handle)
    mutates = args.operation in ('setup', 'settings')
    if mutates and (scope.get('file_changes') is not True or args.confirm != 'true'):
        raise SyncError(403, 'metadata writes require server file changes and explicit confirm=true')
    parent, name, fd = walk_open(args.root, scope)
    os.close(parent)
    if fd is None:
        raise SyncError(404, 'sync root does not exist; this action does not create directories')
    try:
        if not mutates:
            plan = preview(args.root, fd)[0]
            if args.operation == 'inspect':
                plan.pop('merge_diff', None)
            return plan
        if not args.state:
            raise SyncError(422, 'mutations need the private action state directory')
        with mutation_lock(args.state, args.root):
            plan, merged, ignored_mode, metadata_mode = preview(args.root, fd)
            if args.review != plan['review']:
                raise SyncError(409, 'setup files changed; request a fresh preview before writing')
            if args.operation == 'setup':
                metadata = setup_metadata(plan, args.mode)
                if args.mode == 'gitignore' and plan['merge_diff']:
                    atomic_text(fd, '.gitignore', merged, ignored_mode)
            else:
                if plan['metadata'] is None:
                    raise SyncError(409, 'complete the ignore setup decision before changing settings')
                metadata = plan['metadata']
                changed = False
                for key in ('warn_file_mb', 'warn_snapshot_mb', 'warn_file_count'):
                    value = getattr(args, key)
                    if value:
                        metadata['git'][key] = value
                        changed = True
                if not changed:
                    raise SyncError(422, 'provide at least one nonzero warning threshold; zero leaves it unchanged')
                validate_metadata(metadata)
            atomic_text(fd, METADATA_FILE, json.dumps(metadata, indent=2) + '\n', metadata_mode)
            result = preview(args.root, fd)[0]
            result['changed'] = True
            return result
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('capabilities', 'inspect', 'preview', 'setup', 'settings'))
    parser.add_argument('--scope')
    parser.add_argument('--root')
    parser.add_argument('--state')
    parser.add_argument('--review')
    parser.add_argument('--mode', choices=('gitignore', 'sync-only', 'off'))
    parser.add_argument('--confirm', choices=('true', 'false'), default='false')
    parser.add_argument('--warn-file-mb', type=float, default=0)
    parser.add_argument('--warn-snapshot-mb', type=float, default=0)
    parser.add_argument('--warn-file-count', type=int, default=0)
    args = parser.parse_args(argv)
    try:
        if args.operation in ('setup', 'settings') and (not args.review or len(args.review) != 64):
            raise SyncError(422, 'a current preview review token is required')
        if args.operation == 'setup' and args.mode is None:
            raise SyncError(422, 'choose gitignore, sync-only or off explicitly')
        for value in (args.warn_file_mb, args.warn_snapshot_mb):
            if not math.isfinite(value) or value < 0:
                raise SyncError(422, 'warning thresholds must be nonnegative and finite; zero leaves a value unchanged')
        if args.warn_file_count < 0:
            raise SyncError(422, 'file-count thresholds cannot be negative')
        print(json.dumps(execute(args), allow_nan=False))
        return 0
    except (SyncError, Refused, OSError, ValueError) as e:
        print(json.dumps({'ok': False, 'status': getattr(e, 'status', 403 if isinstance(e, Refused) else 422),
                          'error': str(e)}))
        print(str(e), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
