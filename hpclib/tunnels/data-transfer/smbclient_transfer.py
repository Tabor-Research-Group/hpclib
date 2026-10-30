"""Native smbshell transport: Samba in the transfer image, using its FILE ticket.

No password inputs or rclone authentication are accepted here. Keep command
construction, listing validation and file publication in one place for CLI/jobs.
Samba's text listing is deliberately parsed strictly; unsupported names/output
fail before a transfer instead of silently selecting a different file.
"""
import argparse
import datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import uuid


class TransferError(RuntimeError):
    pass


def checked(value):
    # smbclient has its own command parser; subprocess argument quoting alone
    # does not protect that parser. Semicolons separate commands even in quotes.
    if any(ord(c) < 32 or c in '\\";*?' for c in value):
        raise TransferError(f"smbclient cannot safely address this name: {value!r}")
    return value


def path(value):
    value = checked(value.strip('/'))
    if any(p in ('.', '..', '') for p in value.split('/')) and value:
        raise TransferError(f"invalid remote path: {value!r}")
    return value


def quote(value):
    return '"' + checked(str(value)) + '"'


def remote_quote(value):
    # Validate before introducing the SMB path separators ourselves.
    return '"\\' + path(value).replace('/', '\\') + '"'


LISTING = re.compile(r'^  (.{30,}?)([A-Z ]{7})\s+(\d+)\s+'
                     r'([A-Z][a-z]{2} [A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2} \d{4})\s*$')


def listing(text):
    entries, seen = [], set()
    for line in text.splitlines():
        if not line.strip() or re.fullmatch(r'\s*\d+ blocks of size \d+\. \d+ blocks available\s*', line):
            continue
        match = LISTING.fullmatch(line)
        if not match:
            raise TransferError(f"unrecognized smbclient listing: {line!r}")
        name, attrs, size, stamp = match.groups()
        name = name.rstrip()
        if name in ('.', '..'):
            continue
        checked(name)
        if '/' in name or name.casefold() in seen:
            raise TransferError(f"ambiguous remote entry: {name!r}")
        seen.add(name.casefold())
        try:
            date = datetime.datetime.strptime(stamp, '%a %b %d %H:%M:%S %Y').replace(tzinfo=datetime.timezone.utc)
        except ValueError as error:
            raise TransferError(f"unrecognized modification time: {stamp!r}") from error
        entries.append(dict(Path=name, Name=name, Size=int(size), ModTime=date.isoformat(), IsDir='D' in attrs))
    return entries


class Client:
    def __init__(self, args):
        self.timeout, self.binds = args.timeout, args.bind
        self.server = os.environ.get('SMB_HOST', '')
        realm = os.environ.get('SMB_REALM', '')
        spn = os.environ.get('SMB_SPN', '')
        self.explicit_spn = bool(spn)
        self.discovery_attempted = False
        self.name = self.server
        if spn:
            match = re.fullmatch(r'cifs/([A-Za-z0-9_.-]+)(?:@([A-Za-z0-9_.-]+))?', spn, re.I)
            if not match:
                raise TransferError('SMB_SPN must be cifs/HOST or cifs/HOST@REALM')
            self.name, spn_realm = match.groups()
            if spn_realm and realm and spn_realm != realm:
                raise TransferError('SMB_SPN and SMB_REALM specify different realms')
            realm = spn_realm or realm
        # Samba otherwise guesses a realm from the server's DNS suffix, even
        # when krb5.conf has a different default. The transfer ticket is the
        # authority for the default client realm; explicit target realms win.
        principal = getattr(args, 'ticket_principal', '')
        if not realm and '@' in principal:
            realm = principal.rsplit('@', 1)[1]
        for value in (self.server, self.name, realm):
            if value and not re.fullmatch(r'[A-Za-z0-9_.:-]+', value):
                raise TransferError('invalid server name or realm')
        self.realm = realm
        self.address = ''
        if self.name != self.server:
            self.address = self.destination()
        try:
            ipaddress.ip_address(self.name)
        except ValueError:
            pass
        else:
            raise TransferError('Kerberos needs SMB_SPN=cifs/NAME when SMB_HOST is an IP address')
        cache = os.path.abspath(args.cache)
        config = os.path.abspath(args.profile)
        self.prefix = [args.runtime, 'exec', '--bind', f'{Path(cache).parent}:{Path(cache).parent}:rw',
                       '--bind', f'{config}:/etc/hpclib-krb5.conf:ro']
        for bind in self.binds:
            self.prefix.extend(['--bind', bind])
        self.prefix.extend([args.image, 'env', '-u', 'PASSWD', '-u', 'PASSWD_FD', '-u', 'PASSWD_FILE',
                            'LC_ALL=C', 'TZ=UTC', f'KRB5CCNAME=FILE:{cache}',
                            'KRB5_CONFIG=/etc/hpclib-krb5.conf', 'smbclient'])
        self.env = dict(os.environ, LC_ALL='C', TZ='UTC',
                        SINGULARITYENV_KRB5CCNAME=f'FILE:{cache}', APPTAINERENV_KRB5CCNAME=f'FILE:{cache}',
                        SINGULARITYENV_KRB5_CONFIG='/etc/hpclib-krb5.conf',
                        APPTAINERENV_KRB5_CONFIG='/etc/hpclib-krb5.conf')
        for key in tuple(self.env):
            if key.startswith('RCLONE_') or key in ('PASSWD', 'PASSWD_FD', 'PASSWD_FILE'):
                self.env.pop(key)

    def destination(self):
        try:
            records = socket.getaddrinfo(self.server, 445, socket.AF_INET, socket.SOCK_STREAM)
            return records[0][4][0]
        except (OSError, IndexError) as error:
            raise TransferError(f'cannot resolve SMB_HOST {self.server}: {error}') from error

    def run(self, share, command=None, shares=False):
        readonly = ((shares and not command) or command == 'quit'
                    or re.fullmatch(r'ls "[^";\r\n]*"', command or ''))
        args = self.prefix + (['--list=' + self.name, '--grepable'] if shares else ['//' + self.name + '/' + checked(share)])
        # Level 3 explains the generic NT_STATUS_INVALID_PARAMETER failure
        # (often an unknown SPN/guessed realm). Capture it; successful listings
        # still return only stdout and retain their structured JSON output.
        args += ['--port=445', f'--timeout={self.timeout}', '--use-kerberos=required', '--no-pass',
                 '--debuglevel=3' if readonly else '--debuglevel=0']
        if self.address:
            args.append('--ip-address=' + self.address)
        if self.realm:
            args.append('--realm=' + self.realm)
        if command:
            args.append('--command=' + command)
        proc = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, env=self.env,
                              text=True, encoding='utf-8', errors='strict')
        combined = proc.stdout + proc.stderr
        # Some command modes report an SMB failure but return zero. Never allow
        # a later successful command to disguise an earlier failure. Diagnostic
        # stderr can also mention normal handshake statuses; only command output
        # and explicit failure lines count as errors when the process succeeds.
        failure_output = combined if not readonly else proc.stdout + '\n' + '\n'.join(
            line for line in proc.stderr.splitlines()
            if re.match(r'\s*(?:NT_STATUS_|session setup failed:|tree connect failed:)', line, re.I))
        if proc.returncode or re.search(r'\bNT_STATUS_(?!OK\b)[A-Z_]+', failure_output):
            missing_principal = re.search(r'(?:Server principal cifs/\S+ not found|'
                                          r'Server not found in Kerberos database|KDC_ERR_S_PRINCIPAL_UNKNOWN)', combined, re.I)
            short = self.server.rstrip('.').split('.', 1)[0]
            if (missing_principal and readonly and not self.explicit_spn and not self.discovery_attempted
                    and self.realm and '.' in self.server and short != self.name):
                # Only retry a read-only probe after a definite unknown-SPN
                # response. Never replay a write, change an explicit identity,
                # fall back to NTLM/passwords, or change encryption policy.
                self.discovery_attempted = True
                self.address = self.destination()
                self.name = short
                return self.run(share, command, shares)
            identity = f'cifs/{self.name}' + (f'@{self.realm}' if self.realm else '')
            detail = ' '.join(combined.strip()[-2000:].splitlines())
            raise TransferError(f'smbclient failed ({proc.returncode}) for {identity}'
                                f' via {self.address or self.server}: {detail}')
        return proc.stdout

    def ls(self, share, folder):
        if not share:
            result = []
            for line in self.run('', shares=True).splitlines():
                if line.startswith('Disk|'):
                    name = line.split('|', 2)[1]
                    checked(name)
                    result.append(dict(Path=name, Name=name, Size=-1, ModTime=None, IsDir=True))
            return result
        expression = remote_quote(folder)[:-1] + ('\\' if folder else '') + '*"'
        return listing(self.run(share, 'ls ' + expression))

    def lookup(self, share, name):
        if not name:
            self.ls(share, '')
            return dict(IsDir=True, Name='', Size=0, ModTime=None)
        parent, _, leaf = name.rpartition('/')
        entries = [e for e in self.ls(share, parent) if e['Name'].casefold() == leaf.casefold()]
        if len(entries) != 1:
            raise TransferError(f'remote path not found or ambiguous: {name}')
        return entries[0]

    def tree(self, share, folder):
        entries, pending = {}, [('', folder)]
        while pending:
            relative, directory = pending.pop()
            for entry in self.ls(share, directory):
                key = '/'.join(filter(None, [relative, entry['Name']]))
                entries[key] = entry
                if entry['IsDir']:
                    pending.append((key, '/'.join(filter(None, [directory, entry['Name']]))))
        return entries

    def mkdirs(self, share, folder):
        current = ''
        for part in folder.split('/') if folder else []:
            parent = current
            current = '/'.join(filter(None, [current, part]))
            found = [e for e in self.ls(share, parent) if e['Name'].casefold() == part.casefold()]
            if found:
                if len(found) != 1 or not found[0]['IsDir']:
                    raise TransferError(f'not a remote directory: {current}')
            else:
                self.run(share, 'mkdir ' + remote_quote(current))

    def directory_exists(self, share, folder):
        current = ''
        for part in folder.split('/') if folder else []:
            found = [e for e in self.ls(share, current) if e['Name'].casefold() == part.casefold()]
            if not found:
                return False
            if len(found) != 1 or not found[0]['IsDir']:
                raise TransferError(f'not a remote directory: {folder}')
            current = '/'.join(filter(None, [current, found[0]['Name']]))
        return True

    def download(self, share, remote, destination, entry):
        local_directory(destination.parent)
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise TransferError(f'not a regular destination file: {destination}')
        fd, temp = tempfile.mkstemp(prefix='.hpclib-download-', dir=destination.parent)
        os.close(fd)
        try:
            self.run(share, f'get {remote_quote(remote)} {quote(temp)}')
            if os.stat(temp).st_size != entry['Size']:
                raise TransferError(f'download size changed: {remote}')
            if entry['ModTime']:
                stamp = datetime.datetime.fromisoformat(entry['ModTime']).timestamp()
                os.utime(temp, (stamp, stamp))
            os.replace(temp, destination)
        finally:
            Path(temp).unlink(missing_ok=True)

    def upload(self, share, source, remote):
        if source.is_symlink() or not source.is_file():
            raise TransferError(f'not a regular source file: {source}')
        before = source.stat()
        parent, _, _ = remote.rpartition('/')
        temporary = '/'.join(filter(None, [parent, '.hpclib-upload-' + uuid.uuid4().hex]))
        published = False
        try:
            self.run(share, f'put {quote(source)} {remote_quote(temporary)}')
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise TransferError(f'upload source changed: {source}')
            if self.lookup(share, temporary)['Size'] != before.st_size:
                raise TransferError(f'upload size mismatch: {source}')
            self.run(share, f'rename {remote_quote(temporary)} {remote_quote(remote)} -f')
            published = True
        finally:
            # A failed transfer may leave its uniquely named temporary remote file.
            # Cleanup is best effort and must not hide the original exception.
            if not published:
                try:
                    self.run(share, 'del ' + remote_quote(temporary))
                except TransferError:
                    pass


def local_directory(directory):
    directory = Path(directory).absolute()
    for current in reversed([directory, *directory.parents]):
        if current.is_symlink():
            raise TransferError(f'refusing a symbolic-link directory: {current}')
        if current.exists() and not current.is_dir():
            raise TransferError(f'not a local directory: {current}')
    directory.mkdir(parents=True, exist_ok=True)


def local_tree(directory):
    result, seen = {}, set()
    if directory.is_symlink() or not directory.is_dir():
        raise TransferError(f'not a regular directory: {directory}')
    for parent, directories, files in os.walk(directory, followlinks=False):
        for name in directories + files:
            checked(name)
            item = Path(parent) / name
            relative = item.relative_to(directory).as_posix()
            if relative.casefold() in seen:
                raise TransferError(f'case-insensitive SMB name collision: {relative}')
            seen.add(relative.casefold())
            if item.is_symlink() or not (item.is_file() or item.is_dir()):
                raise TransferError(f'refusing a link or special file: {item}')
            result[relative] = dict(IsDir=item.is_dir(), Size=item.stat().st_size)
    return result


def execute(client, args):
    remote = path(args.remote)
    share, _, folder = remote.partition('/')
    if args.command in ('ls', 'check'):
        entries = client.ls(share, folder)
        if args.command == 'ls':
            if args.json:
                print(json.dumps(entries, ensure_ascii=False))
            else:
                for entry in entries:
                    print(entry['Name'] + ('/' if entry['IsDir'] else ''))
        return
    if not share:
        raise TransferError('a transfer needs a share, not just the server')
    local = Path(args.local).absolute()
    checked(str(local))
    pull = args.command == 'get' or args.command == 'sync-pull'
    sync = args.command.startswith('sync-')
    if pull:
        root = client.lookup(share, folder)
        source = client.tree(share, folder) if root['IsDir'] else {root['Name']: root}
        target = local_tree(local) if sync and local.exists() else {}
    else:
        source = local_tree(local) if local.is_dir() else {checked(local.name): dict(IsDir=False, Size=local.stat().st_size)}
        if local.is_symlink():
            raise TransferError('refusing a symbolic-link source')
        target = client.tree(share, folder) if sync and client.directory_exists(share, folder) else {}
    # Enumerate and validate everything before changing destinations. Sync does
    # not delete files until every source file has transferred successfully.
    for key, item in source.items():
        path(key)
        if key in target and target[key]['IsDir'] != item['IsDir']:
            raise TransferError(f'file/directory conflict: {key}')
    if not args.dry_run:
        if pull:
            local_directory(local)
        else:
            client.mkdirs(share, folder)
    for key, item in sorted(source.items(), key=lambda pair: (pair[0].count('/'), pair[0])):
        remote_file = '/'.join(filter(None, [folder, key]))
        if pull and not root['IsDir']:
            remote_file = folder
        print(('Would copy ' if args.dry_run else 'Copying ') + key, file=sys.stderr, flush=True)
        if args.dry_run:
            continue
        if item['IsDir']:
            local_directory(local / key) if pull else client.mkdirs(share, remote_file)
        elif pull:
            client.download(share, remote_file, local / key, item)
        else:
            source_file = local / key if local.is_dir() else local
            client.upload(share, source_file, remote_file)
    if sync:
        extra = sorted(set(target) - set(source), key=lambda key: (-key.count('/'), key))
        for key in extra:
            print(('Would remove ' if args.dry_run else 'Removing ') + key, file=sys.stderr)
            if not args.dry_run:
                if pull:
                    item = local / key
                    item.rmdir() if target[key]['IsDir'] else item.unlink()
                else:
                    operation = 'rmdir ' if target[key]['IsDir'] else 'del '
                    client.run(share, operation + remote_quote('/'.join(filter(None, [folder, key]))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--cache', required=True)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--bind', action='append', default=[])
    parser.add_argument('--timeout', type=int, default=30)
    parser.add_argument('--ticket-principal', default='', help='public principal metadata from this transfer cache')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('command', choices=('ls', 'check', 'get', 'put', 'sync-pull', 'sync-push'))
    parser.add_argument('remote')
    parser.add_argument('local', nargs='?')
    args = parser.parse_args()
    if args.command not in ('ls', 'check') and not args.local:
        parser.error('a transfer needs a local path')
    try:
        execute(Client(args), args)
    except (OSError, ValueError, TransferError) as error:
        parser.exit(1, f'smbshell: {error}\n')


if __name__ == '__main__':
    main()
