#!/usr/bin/env python3
"""Scoped Data transfer metadata and queued console sync controls; Python 3.6-safe.

capabilities | inspect | preview | setup | settings
Read operations do not create root files. Mutations need a matching preview, explicit
confirmation, files:write scope, and a scope file affirming server file changes are enabled.
Metadata commands never change console registrations or Git history. Agent commands
are scoped requests; only the running console's consent-enabled manager executes them.
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
import time
import uuid

from action_paths import Refused, walk_open, check_scope
from sync_manager import read_setup_file
from sync_spec import (METADATA_FILE, SyncError, ignore_setup_plan, setup_metadata, validate_metadata, identifier)

AGENT_OPS = ('list', 'status', 'plan', 'review', 'approve-snapshot', 'apply', 'retry', 'pause', 'resume', 'configure')
AGENT_READS = ('list', 'status', 'plan')
MAX_RECORD = 16 << 20


def agent_directory(state):
    return os.path.join(state, 'sync', 'agent-requests')


def agent_read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECORD:
            raise SyncError(422, 'invalid sync action record')
        with os.fdopen(fd, 'r', closefd=False) as handle:
            return json.load(handle)
    finally:
        os.close(fd)


def agent_write(directory, name, value):
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        text = json.dumps(value, allow_nan=False) + '\n'
        if len(text.encode()) > MAX_RECORD:
            raise SyncError(413, 'sync action result is too large')
        atomic_text(fd, name, text, 0o600)
    finally:
        os.close(fd)


def agent_ready(state):
    item = agent_read(os.path.join(agent_directory(state), 'presence.json')) if state else None
    return bool(item and 0 <= time.time() - item.get('at', 0) < 30)


def agent_root_checkpoint(root, fd):
    """Bind queued work to this directory without node-local NFS device numbers.

    A directory metadata change while queued also requires a fresh request. Inode
    plus ctime catches replacement/reuse; the descriptor walk still rejects links.
    """
    info = os.fstat(fd)
    return [root, 'inode-ctime-v1', info.st_ino, info.st_ctime_ns]


def agent_action(args, scope, fd):
    if not args.state or not isinstance(scope.get('actor'), str) or not scope['actor']:
        raise SyncError(403, 'sync management needs an identified tunnel action scope')
    operation = args.operation[len('agent-'):]
    directory = agent_directory(args.state)
    if operation == 'request-status':
        ticket = identifier(args.ticket)
        with mutation_lock(args.state, 'agent-requests'):
            item = agent_read(os.path.join(directory, ticket + '.json'))
            if item and item['state'] in ('queued', 'claimed') and item['expires'] <= time.time():
                admitted = item['state'] == 'claimed'
                item = dict(item, state='uncertain' if admitted else 'expired', reply={'ok': False, 'status': 409,
                    'error': 'request expired after dispatch; inspect sync status before issuing a fresh command' if admitted else
                             'request expired before dispatch; submit a fresh request'})
        if item is None or item.get('actor') != scope['actor']:
            raise SyncError(404, 'no sync request with that ticket for this token')
        for key in ('roots', 'deny'):
            normalize = lambda value: None if value is None else sorted(value)
            if normalize(item['scope'].get(key)) != normalize(scope.get(key)):
                raise SyncError(403, 'request scope changed; issue a fresh query')
        check_scope(item['root'], scope)
        if os.path.commonpath([args.root, item['root']]) != args.root:
            raise SyncError(403, 'ticket is outside the selected directory')
        return {k: item.get(k) for k in ('ticket', 'operation', 'state', 'created', 'expires', 'reply')}
    if operation not in AGENT_OPS:
        raise SyncError(422, 'unknown sync management operation')
    if not agent_ready(args.state):
        raise SyncError(409, 'open the local console, connect this cluster, and Allow agent access for a sync')
    mutates = operation not in AGENT_READS
    if mutates and (scope.get('file_changes') is not True or args.confirm != 'true'):
        raise SyncError(403, 'sync mutations require file changes and explicit confirm=true')
    values = {}
    if operation != 'list':
        values['id'] = identifier(args.id)
    if operation in ('plan', 'approve-snapshot', 'apply', 'retry'):
        values['plan'] = identifier(args.plan)
    if mutates:
        if args.revision < 1:
            raise SyncError(422, 'provide the current sync revision')
        values.update(revision=args.revision, confirm=True)
    if operation == 'configure':
        values['settings'] = {k: getattr(args, k) for k in ('warn_file_mb', 'warn_snapshot_mb', 'warn_file_count') if getattr(args, k)}
        if not values['settings']:
            raise SyncError(422, 'provide at least one nonzero warning threshold')
    with mutation_lock(args.state, 'agent-requests'):
        os.makedirs(directory, mode=0o700, exist_ok=True)
        records = [agent_read(os.path.join(directory, name)) for name in os.listdir(directory)
                   if name.startswith('a-') and name.endswith('.json')]
        owned = [r for r in records if r and r.get('actor') == scope['actor']]
        pending = [r for r in owned if r['state'] in ('queued', 'claimed') and r['expires'] > time.time()]
        if len(pending) >= 32:
            raise SyncError(429, 'too many pending sync requests; wait for the console')
        finished = sorted((r for r in owned if r not in pending), key=lambda r: r['created'])
        for old in finished[:-127]:
            os.unlink(os.path.join(directory, old['ticket'] + '.json'))
        ticket = 'a-' + uuid.uuid4().hex
        item = {'v': 1, 'ticket': ticket, 'actor': scope['actor'], 'operation': operation, 'args': values,
                'root': args.root, 'identity': agent_root_checkpoint(args.root, fd), 'scope': scope,
                'created': time.time(), 'expires': time.time() + 300, 'state': 'queued'}
        agent_write(directory, ticket + '.json', item)
    return {'ticket': ticket, 'state': 'queued', 'operation': operation, 'expires': item['expires'],
            'message': 'The running local console processes this request. Poll sync_query operation=request-status; follow accepted runs with operation=status.'}


def agent_exchange(request, state):
    """Owner-only RPC over the existing SSH master; no public callback port or console credentials."""
    operation = request['operation']
    consumer = identifier(request.get('consumer'))
    directory = agent_directory(state)
    if operation == 'agent-poll' and not os.path.isdir(directory):
        agent_write(directory, 'presence.json', {'at': time.time(), 'consumer': consumer})
        return {'requests': []}
    with mutation_lock(state, 'agent-requests'):
        if operation == 'agent-reply':
            ticket = identifier(request.get('ticket'))
            item = agent_read(os.path.join(directory, ticket + '.json'))
            if not item or item.get('consumer') != consumer or item['state'] != 'claimed':
                raise SyncError(409, 'sync request lease belongs to another console or is complete')
            reply = request.get('reply')
            if not isinstance(reply, dict) or type(reply.get('ok')) is not bool:
                raise SyncError(422, 'invalid sync request reply')
            item.update(state='succeeded' if reply['ok'] else 'failed', reply=reply, finished=time.time())
            agent_write(directory, ticket + '.json', item)
            return {'ticket': ticket, 'state': item['state']}
        if operation != 'agent-poll':
            raise SyncError(422, 'unknown agent queue RPC')
        agent_write(directory, 'presence.json', {'at': time.time(), 'consumer': consumer})
        ready = []
        for name in sorted(os.listdir(directory)):
            if not name.startswith('a-') or not name.endswith('.json'):
                continue
            item = agent_read(os.path.join(directory, name))
            if item['state'] not in ('queued', 'claimed'):
                continue
            if item['expires'] <= time.time():
                admitted = item['state'] == 'claimed'
                item.update(state='uncertain' if admitted else 'expired', reply={'ok': False, 'status': 409,
                    'error': 'request expired after dispatch; inspect sync status before issuing a fresh command' if admitted else
                             'request expired before dispatch; submit a fresh request'})
                agent_write(directory, name, item)
                continue
            if item['state'] == 'claimed' and item.get('lease_until', 0) > time.time():
                continue
            try:
                parent, basename, fd = walk_open(item['root'], item['scope'])
                os.close(parent)
                if fd is None:
                    raise SyncError(404, 'request directory no longer exists')
                try:
                    if item['identity'] != agent_root_checkpoint(item['root'], fd):
                        raise SyncError(409, 'request directory changed or uses an older checkpoint; submit a fresh request')
                finally:
                    os.close(fd)
            except (OSError, Refused, SyncError) as error:
                item.update(state='failed', reply={'ok': False, 'status': 409, 'error': str(error)})
                agent_write(directory, name, item)
                continue
            item.update(state='claimed', consumer=consumer, lease_until=time.time() + 30,
                        valid_for=min(300, max(0, item['expires'] - time.time())))
            agent_write(directory, name, item)
            ready.append(item)
            if len(ready) >= 4:
                break
        return {'requests': ready}


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
                'transfers_enabled': False, 'agent_ready': agent_ready(args.state), 'agent_protocol': True}
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
        if args.operation.startswith('agent-'):
            return agent_action(args, scope, fd)
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
    parser.add_argument('operation', choices=('capabilities', 'inspect', 'preview', 'setup', 'settings', 'agent', 'agent-request-status') +
                        tuple('agent-' + op for op in AGENT_OPS))
    parser.add_argument('--scope')
    parser.add_argument('--root')
    parser.add_argument('--state')
    parser.add_argument('--review')
    parser.add_argument('--mode', choices=('gitignore', 'sync-only', 'off'))
    parser.add_argument('--confirm', choices=('true', 'false'), default='false')
    parser.add_argument('--warn-file-mb', type=float, default=0)
    parser.add_argument('--warn-snapshot-mb', type=float, default=0)
    parser.add_argument('--warn-file-count', type=int, default=0)
    parser.add_argument('--id')
    parser.add_argument('--revision', type=int, default=0)
    parser.add_argument('--plan')
    parser.add_argument('--ticket')
    parser.add_argument('--command', choices=AGENT_OPS + ('request-status',))
    args = parser.parse_args(argv)
    try:
        if args.operation == 'agent':
            if args.command is None:
                raise SyncError(422, 'choose an agent sync command')
            args.operation = 'agent-' + args.command
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
